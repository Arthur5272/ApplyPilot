# Changelog

All notable changes to ApplyPilot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1+custom.1] - 2026-10-14

Custom fork release ([Arthur5272/ApplyPilot](https://github.com/Arthur5272/ApplyPilot)), based on upstream 0.3.0.

### Added
- **LLM model fallback rotation** - new `LLM_MODEL_FALLBACKS` env var (comma-separated
  models on the same provider/API key). When a model exhausts its retry ladder the client
  rotates to the next model in the pool instead of failing the run.
- **Gemini daily-quota detection** - a 429 whose body mentions a `PerDay` quota marks that
  model as exhausted for the rest of the process, so it is skipped instead of retried.

### Changed
- **Rate-limit backoff** - retries on 429/503/timeouts now use exponential shares
  (1:2:4:8) that add up to a 7-minute total budget per model, replacing the old
  10s-doubling schedule capped at 60s. Rotation only happens after the full budget is
  spent, so short per-minute throttles don't abandon a healthy model.
- **Incremental DB persistence** - scoring, resume tailoring and cover-letter generation
  now commit after every job instead of once at the end of the batch. An interrupted run
  (Ctrl+C, quota exhaustion, crash) keeps all work done so far.

### Fixed
- **Failed scores stuck at 0** - when the LLM call fails, `fit_score` is left `NULL` so
  the job stays pending and is retried on the next run, instead of being stored as a fake 0.
- **`<thought>` blocks breaking JSON extraction** - smart extract now strips
  `<thought>...</thought>` output (Gemini thinking models), in addition to `<think>`.
- **Chrome failing silently without a display** - auto-apply forces headless Chrome when
  neither `DISPLAY` nor `WAYLAND_DISPLAY` is set, instead of surfacing confusing
  `browser_unavailable` / connection-refused errors on every job.
- **Fork tags triggering PyPI publish** - the publish workflow only runs on the upstream
  repository.

## [0.2.0] - 2026-02-17

### Added
- **Parallel workers for discovery/enrichment** - `applypilot run --workers N` enables
  ThreadPoolExecutor-based parallelism for Workday scraping, smart extract, and detail
  enrichment. Default is sequential (1); power users can scale up.
- **Apply utility modes** - `--gen` (generate prompt for manual debugging), `--mark-applied`,
  `--mark-failed`, `--reset-failed` flags on `applypilot apply`
- **Dry-run mode** - `applypilot apply --dry-run` fills forms without clicking Submit
- **5 new tracking columns** - `agent_id`, `last_attempted_at`, `apply_duration_ms`,
  `apply_task_id`, `verification_confidence` for better apply-stage observability
- **Manual ATS detection** - `manual_ats` list in `config/sites.yaml` skips sites with
  unsolvable CAPTCHAs (e.g. TCS iBegin)
- **Qwen3 `/no_think` optimization** - automatically saves tokens when using Qwen models
- **`config.DEFAULTS`** - centralized dict for magic numbers (`min_score`, `max_apply_attempts`,
  `poll_interval`, `apply_timeout`, `viewport`)

### Fixed
- **Config YAML not found after install** - moved `config/` into the package at
  `src/applypilot/config/` so YAML files (employers, sites, searches) ship with `pip install`
- **Search config format mismatch** - wizard wrote `searches:` key but discovery code
  expected `queries:` with tier support. Aligned wizard output and example config
- **JobSpy install isolation** - removed python-jobspy from package dependencies due to
  broken numpy==1.26.3 exact pin in jobspy metadata. Installed separately with `--no-deps`
- **Scoring batch limit** - default limit of 50 silently left jobs unscored across runs.
  Changed to no limit (scores all pending jobs in one pass)
- **Missing logging output** - added `logging.basicConfig(INFO)` so per-job progress for
  scoring, tailoring, and cover letters is visible during pipeline runs

### Changed
- **Blocked sites externalized** - moved from hardcoded sets in launcher.py to
  `config/sites.yaml` under `blocked:` key
- **Site base URLs externalized** - moved from hardcoded dict in detail.py to
  `config/sites.yaml` under `base_urls:` key
- **SSO domains externalized** - moved from hardcoded list in prompt.py to
  `config/sites.yaml` under `blocked_sso:` key
- **Prompt improvements** - screening context uses `target_role` from profile,
  salary section includes `currency_conversion_note` and dynamic hourly rate examples
- **`acquire_job()` fixed** - writes `agent_id` and `last_attempted_at` to proper columns
  instead of misusing `apply_error`
- **`profile.example.json`** - added `currency_conversion_note` and `target_role` fields

## [0.1.0] - 2026-02-17

### Added
- 6-stage pipeline: discover, enrich, score, tailor, cover letter, apply
- Multi-source job discovery: Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs
- Workday employer portal support (46 preconfigured employers)
- Direct career site scraping (28 preconfigured sites)
- 3-tier job description extraction cascade (JSON-LD, CSS selectors, AI fallback)
- AI-powered job scoring (1-10 fit scale with rationale)
- Resume tailoring with factual preservation (no fabrication)
- Cover letter generation per job
- Autonomous browser-based application submission via Playwright
- Interactive setup wizard (`applypilot init`)
- Cross-platform Chrome/Chromium detection (Windows, macOS, Linux)
- Multi-provider LLM support (Gemini, OpenAI, local models via OpenAI-compatible endpoints)
- Pipeline stats and HTML results dashboard
- YAML-based configuration for employers, career sites, and search queries
- Job deduplication across sources
- Configurable score threshold filtering
- Safety limits for maximum applications per run
- Detailed application results logging
