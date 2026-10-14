"""
Unified LLM client for ApplyPilot.

Auto-detects provider from environment:
  GEMINI_API_KEY  -> Google Gemini (default: gemini-2.0-flash)
  OPENAI_API_KEY  -> OpenAI (default: gpt-4o-mini)
  LLM_URL         -> Local llama.cpp / Ollama compatible endpoint

LLM_MODEL env var overrides the model name for any provider.
"""

import logging
import os
import time

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _detect_provider() -> tuple[str, str, str, list[str]]:
    """Return (base_url, model, api_key, fallback_models) based on environment variables.

    Reads env at call time (not module import time) so that load_env() called
    in _bootstrap() is always visible here.

    LLM_MODEL_FALLBACKS is a comma-separated list of additional models to
    rotate through (same provider/base_url) when the primary model hits its
    daily quota -- e.g. multiple free-tier Gemini models, each with its own
    independent daily request budget.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    local_url = os.environ.get("LLM_URL", "")
    model_override = os.environ.get("LLM_MODEL", "")
    fallbacks = [
        m.strip() for m in os.environ.get("LLM_MODEL_FALLBACKS", "").split(",") if m.strip()
    ]

    if gemini_key and not local_url:
        return (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            model_override or "gemini-2.0-flash",
            gemini_key,
            fallbacks,
        )

    if openai_key and not local_url:
        return (
            "https://api.openai.com/v1",
            model_override or "gpt-4o-mini",
            openai_key,
            fallbacks,
        )

    if local_url:
        return (
            local_url.rstrip("/"),
            model_override or "local-model",
            os.environ.get("LLM_API_KEY", ""),
            fallbacks,
        )

    raise RuntimeError(
        "No LLM provider configured. "
        "Set GEMINI_API_KEY, OPENAI_API_KEY, or LLM_URL in your environment."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5
_TIMEOUT = 120  # seconds

# Total time spent waiting on 429/503 for a single model before giving up on
# it and rotating to the next one in the pool. Split across the N-1 wait slots
# (the last attempt doesn't wait) as an exponential share (1:2:4:8:...) so it
# still backs off, but the SUM across all cycles is the number below -- not
# each individual cycle capped at it.
_RATE_LIMIT_TOTAL_WAIT = 420  # 7 min


def _backoff_schedule(n_waits: int, total: float) -> list[float]:
    """Exponential shares (1, 2, 4, ...) that sum to exactly `total`."""
    if n_waits <= 0:
        return []
    weights = [2 ** i for i in range(n_waits)]
    share = total / sum(weights)
    return [w * share for w in weights]


_BACKOFF_SCHEDULE = _backoff_schedule(_MAX_RETRIES - 1, _RATE_LIMIT_TOTAL_WAIT)


_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"


def _is_daily_quota_error(resp: httpx.Response) -> bool:
    """Detect a 429 caused by a *daily* quota (RPD), not a per-minute throttle.

    Waiting out a per-minute rate limit makes sense; waiting out a daily quota
    doesn't -- it won't reset for hours. Google's error body names the quota
    (e.g. "GenerateRequestsPerDayPerProjectPerModel-FreeTier"), so we just
    look for "PerDay" in the raw response text rather than depend on the
    exact JSON shape.
    """
    try:
        return "PerDay" in resp.text
    except Exception:
        return False


class LLMClient:
    """Thin LLM client supporting OpenAI-compatible and native Gemini endpoints.

    For Gemini keys, starts on the OpenAI-compat layer. On a 403 (which
    happens with preview/experimental models not exposed via compat), it
    automatically switches to the native generateContent API and stays there
    for the lifetime of the process.

    Supports rotating across multiple models (fallback_models) that share the
    same provider/API key but have independent daily quotas -- when one hits
    RESOURCE_EXHAUSTED for the day, the client switches to the next and
    remembers not to try the exhausted one again for the rest of the process.
    """

    def __init__(self, base_url: str, model: str, api_key: str,
                 fallback_models: list[str] | None = None) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=_TIMEOUT)
        # True once we've confirmed the native Gemini API works for this model
        self._use_native_gemini: bool = False
        self._is_gemini: bool = base_url.startswith(_GEMINI_COMPAT_BASE)

        # Ordered pool of models to try: primary first, then fallbacks (deduped).
        self._model_pool: list[str] = [model] + [
            m for m in (fallback_models or []) if m != model
        ]
        self._daily_exhausted: set[str] = set()

    def _next_available_model(self, exclude: set[str] | None = None) -> str | None:
        """First pool model that isn't daily-exhausted or in `exclude`, or None."""
        exclude = exclude or set()
        for m in self._model_pool:
            if m not in self._daily_exhausted and m not in exclude:
                return m
        return None

    # -- Native Gemini API --------------------------------------------------

    def _chat_native_gemini(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the native Gemini generateContent API.

        Used automatically when the OpenAI-compat endpoint returns 403,
        which happens for preview/experimental models not exposed via compat.

        Converts OpenAI-style messages to Gemini's contents/systemInstruction
        format transparently.
        """
        contents: list[dict] = []
        system_parts: list[dict] = []

        for msg in messages:
            role = msg["role"]
            text = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": text}]})
            elif role == "assistant":
                # Gemini uses "model" instead of "assistant"
                contents.append({"role": "model", "parts": [{"text": text}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        url = f"{_GEMINI_NATIVE_BASE}/models/{self.model}:generateContent"
        resp = self._client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": self.api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    # -- OpenAI-compat API --------------------------------------------------

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the OpenAI-compatible endpoint."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )

        # 403 on Gemini compat = model not available on compat layer.
        # Raise a specific sentinel so chat() can switch to native API.
        if resp.status_code == 403 and self._is_gemini:
            raise _GeminiCompatForbidden(resp)

        return self._handle_compat_response(resp)

    @staticmethod
    def _handle_compat_response(resp: httpx.Response) -> str:
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    # -- public API ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request and return the assistant message text.

        Retries 429/503/timeouts against the CURRENT model first, with backoff
        waits summing to _RATE_LIMIT_TOTAL_WAIT across the retry ladder. Only
        after that model's full retry budget is exhausted does it rotate to
        the next model in the pool -- a brief per-minute throttle should
        clear well within the ladder, so rotating on the first 429 would just
        abandon a perfectly fine model prematurely.
        """
        tried_models: set[str] = set()

        while True:
            # If the current model already hit its daily quota earlier in this
            # run, don't waste a full retry ladder on it -- skip straight ahead.
            if self.model in self._daily_exhausted:
                nxt = self._next_available_model(exclude=tried_models)
                if nxt is None:
                    raise RuntimeError(
                        f"All configured models hit their daily quota: "
                        f"{sorted(self._daily_exhausted)}. Add more models to "
                        f"LLM_MODEL_FALLBACKS or wait for quotas to reset."
                    )
                self.model = nxt

            # Qwen3 optimization: prepend /no_think to skip chain-of-thought
            # reasoning, saving tokens on structured extraction tasks.
            if "qwen" in self.model.lower() and messages:
                first = messages[0]
                if first.get("role") == "user" and not first["content"].startswith("/no_think"):
                    messages = [{"role": first["role"], "content": f"/no_think\n{first['content']}"}] + messages[1:]

            last_exc: Exception | None = None

            for attempt in range(_MAX_RETRIES):
                try:
                    # Route to native Gemini if we've already confirmed it's needed
                    if self._use_native_gemini:
                        return self._chat_native_gemini(messages, temperature, max_tokens)

                    return self._chat_compat(messages, temperature, max_tokens)

                except _GeminiCompatForbidden as exc:
                    # Model not available on OpenAI-compat layer — switch to native.
                    log.warning(
                        "Gemini compat endpoint returned 403 for model '%s'. "
                        "Switching to native generateContent API. "
                        "(Preview/experimental models are often compat-only on native.)",
                        self.model,
                    )
                    self._use_native_gemini = True
                    # Retry immediately with native — don't count as a rate-limit wait
                    try:
                        return self._chat_native_gemini(messages, temperature, max_tokens)
                    except httpx.HTTPStatusError as native_exc:
                        raise RuntimeError(
                            f"Both Gemini endpoints failed. Compat: 403 Forbidden. "
                            f"Native: {native_exc.response.status_code} — "
                            f"{native_exc.response.text[:200]}"
                        ) from native_exc

                except httpx.HTTPStatusError as exc:
                    resp = exc.response
                    last_exc = exc

                    if resp.status_code not in (429, 503):
                        raise

                    if attempt < _MAX_RETRIES - 1:
                        wait = _BACKOFF_SCHEDULE[attempt]
                        log.warning(
                            "LLM rate limited (HTTP %s) on model '%s'. Waiting %.0fs "
                            "before retry %d/%d.",
                            resp.status_code, self.model, wait, attempt + 1, _MAX_RETRIES,
                        )
                        time.sleep(wait)
                        continue
                    break  # retry ladder exhausted for this model -- fall through to rotation

                except httpx.TimeoutException as exc:
                    last_exc = exc
                    if attempt < _MAX_RETRIES - 1:
                        wait = _BACKOFF_SCHEDULE[attempt]
                        log.warning(
                            "LLM request timed out on model '%s', retrying in %.0fs "
                            "(attempt %d/%d)",
                            self.model, wait, attempt + 1, _MAX_RETRIES,
                        )
                        time.sleep(wait)
                        continue
                    break

            # Retry ladder exhausted for this model -- try the next one, if any.
            tried_models.add(self.model)
            if (
                isinstance(last_exc, httpx.HTTPStatusError)
                and last_exc.response.status_code == 429
                and self._is_gemini
                and _is_daily_quota_error(last_exc.response)
            ):
                # Confirmed daily quota, not a transient throttle -- don't
                # retry this model again for the rest of the run.
                self._daily_exhausted.add(self.model)

            nxt = self._next_available_model(exclude=tried_models)
            if nxt is not None:
                log.warning(
                    "Model '%s' exhausted its retry budget. Switching to '%s'.",
                    self.model, nxt,
                )
                self.model = nxt
                continue

            if last_exc:
                raise last_exc
            raise RuntimeError("LLM request failed after all retries")

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


class _GeminiCompatForbidden(Exception):
    """Sentinel: Gemini OpenAI-compat returned 403. Switch to native API."""
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Gemini compat 403: {response.text[:200]}")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: LLMClient | None = None


def get_client() -> LLMClient:
    """Return (or create) the module-level LLMClient singleton."""
    global _instance
    if _instance is None:
        base_url, model, api_key, fallbacks = _detect_provider()
        log.info(
            "LLM provider: %s  model: %s%s",
            base_url, model,
            f"  fallbacks: {fallbacks}" if fallbacks else "",
        )
        _instance = LLMClient(base_url, model, api_key, fallback_models=fallbacks)
    return _instance
