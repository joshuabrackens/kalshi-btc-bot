# Phase 4–6 readiness — current implementation

This note describes the current source snapshot. For setup, use `BTC_Bot_All_Phases_Environment_Quickstart.md` and `DEPLOYMENT_PHASE1_TO_6.md`.

## Phase 4 — market discovery and research-to-paper

- `market_catalog_scanner.py` paginates Kalshi open events with nested markets and official category metadata under hard page/market caps, explicit partial/failure state, and an activity-ranked candidate pool.
- `market_decision_pipeline.py` rotates through the bounded pool, gets current market details/order book, and requests a calibrated probability plus source-attributed current evidence from the separately deployed HTTPS provider service.
- `market_analysis_provider_service.py` source is included. Deploy it as a separate HTTPS service with an account-owned OpenAI API key, a shared bearer token, and the shared database. It returns evidence and candidate probabilities, not an order or authorization to trade.
- `market_decision_engine.py` checks category allowlist, market state, exact ticker binding, evidence freshness, out-of-sample validation sample count/Brier score, volume, depth, spread, estimated fees/slippage, net edge, entry cap, and exposure limits.
- Resolved YES/NO outcomes are collected after a minimum age and used for model/category/version calibration. It needs 100 subsequent resolved outcomes, adequate samples for both sides, and Brier score <= 0.22 before that calibration gate passes. This warm-up is intentional; do not fake a validation registry.
- Eligible opportunities may be recorded in the separate research paper journal only when `MARKET_AUTOPAPER_ENABLED=true`, runtime is paper-only, category is allowlisted, provider/evidence/calibration pass, and the dashboard master toggle is enabled.
- The inventory scanner sees up to its configured limits; deep analysis rotates over an activity-ranked pool and a smaller batch per cycle. It does not deeply analyze every market each cycle.

## Phase 5 — multi-user security foundation

Password/session primitives, encrypted credential-vault support and readiness checks exist. This is not a completed production multi-user app. Login middleware, account recovery, full authorization review, per-user trade ownership, database tenant isolation, and per-user risk enforcement must be completed before shared multi-user deployment.

## Phase 6 — real-order readiness

The credential panel stores/validates a Kalshi API Key ID and matching private key encrypted. The low-level `kalshi_order_adapter.py` has Demo-only defaults and mocked unit coverage, but it is not registered as the application live-execution path. `REAL_ORDER_ADAPTER_INSTALLED` and `LIVE_APPROVAL_RECORDED` remain false. Live order placement is blocked even if an environment variable is changed. Before considering real trading, complete end-to-end Kalshi Demo order lifecycle tests (submit/fill/partial fill, cancel/reconcile, timeout/restart, duplicate prevention, fees/positions, pause/kill switch), code review, and separate explicit approval.

## Win-rate improvement

The reported baseline was approximately 95 paper trades at 56% win rate. This is a noisy sample, not evidence the system is profitable or that the next filter will improve it. The project includes outcome collection, historical replay/audit tools, cohort analysis, Brier calibration, fee-aware expectancy, profit factor and drawdown evaluation. Avoid tuning only headline hit rate or fitting filters to 95 trades; require chronological out-of-sample improvements.

## Current validation status

See `BTC_BOT_MARKET_PIPELINE_VALIDATION.md` for the latest 233-test result and explicit limitations. Local regression and offline stress checks passed. The required two-hour soak did not complete in this execution environment; actual Render deployment and the user's production trade-journal replay remain pending. Keep `TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false`.


### Kalshi scanner recovery and diagnostics

The scanner uses bounded HTTP retries and a slower public-data request cadence. On a failure, the dashboard now reports the upstream HTTP status and failed page number without exposing the opaque pagination cursor. Transient 408/425/429/5xx/network failures are retried; a rejected continuation cursor triggers one clean restart, and persistent failures resume automatically from the last safe cursor while the dashboard autopilot is enabled. If a scan still fails, inspect the reported HTTP status and `failed_page_number` before changing credentials. Public market inventory does not require a Kalshi private key.
