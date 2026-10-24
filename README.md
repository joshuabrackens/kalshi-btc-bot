# Kalshi BTC 15-Minute Paper Bot

> **Current Phase 1–6 snapshot (2026-10-23):** see [`DEPLOYMENT_PHASE1_TO_6.md`](DEPLOYMENT_PHASE1_TO_6.md) for exact Render settings and upload steps, and [`PHASE456_VALIDATION.md`](PHASE456_VALIDATION.md) for local test status. The scanner catalogs open markets within explicit safety caps. The encrypted Kalshi credential vault does not enable real orders; Real mode remains locked and the worker is paper-only.


## Purpose and safety boundary

This repository implements a BTC-focused, paper-only Kalshi decision and journal system. It consumes fresh TradingView/GainzAlgo alerts, evaluates closed BTC candles across 1m/5m/15m timeframes, checks a continuously refreshed chart-trend snapshot, validates current Kalshi order-book quotes, and records simulated entries/exits and official settlement outcomes.

**Real order placement is not implemented.** The worker refuses to run unless `TRADING_MODE=paper` and `LIVE_TRADING_ENABLED` is false. Evidence scores are not calibrated win probabilities, and small paper samples do not establish a tradable edge.

Recommended production settings (on both Render services where applicable):

```text
TRADING_MODE=paper
LIVE_TRADING_ENABLED=false
PAPER_SIGNAL_SOURCE=hybrid
PAPER_MAX_ENTRY_CENTS=92
PAPER_ENTRY_TARGET_MAX_CENTS=80
MAX_TRADE_DOLLARS=1.00
MAX_DAILY_LOSS_DOLLARS=10.00
PAPER_WORKER_INTERVAL_SECONDS=15
BTC_TREND_MONITOR_INTERVAL_SECONDS=30
BTC_TREND_MONITOR_MAX_CACHE_AGE_SECONDS=45
```

The 92¢ value is the hard cap; 80¢ is a soft preferred entry target, not an entry blocker. If a fresh directional signal passes the hybrid major-flaw check, executable quote/spread checks, time-window validation, and risk/budget checks, the worker may enter above 80¢ through 92¢. Above 92¢ it waits and does not open a position. The adaptive learner can tighten the displayed preference, but that preference does not block an otherwise approved signal. Expensive contracts leave much less upside, and the direction/evidence score is not a calibrated probability of winning.

## Architecture

- `main.py`: FastAPI web service, signed TradingView/GainzAlgo webhook, dashboard APIs, paper journal endpoints, diagnostics and analytics.
- `paper_worker.py`: paper-only worker, fresh-alert validation, order-book pricing, trend fusion, entry/risk checks, exits, official settlement and retrospective rejected-signal review.
- `candle_feed.py`: public Binance closed-candle data.
- `btc_trend_monitor.py`: multi-timeframe chart evidence (trend/momentum/volume/structure/volatility); it is diagnostic evidence, not a probability estimate.
- `signal_engine.py` / `signal_fusion.py`: internal strategy diagnostics and TradingView-primary hybrid gate.
- `kalshi_api.py` / `market_discovery.py`: public Kalshi market discovery, current order-book quotes and market results.
- `db_store.py`: shared SQLite/PostgreSQL adapter and schema migrations.
- `performance_analytics.py`: descriptive historical P&L and decision cohort analysis.
- `trade_learning.py`: idempotent per-settled-trade feature snapshots, outcome reviews, cohort insights, and a bounded paper-only adaptive entry-target policy. It can tighten the preferred target after repeated poor high-price hybrid outcomes; it never loosens the target or changes the hard cap/dollar risk limits.
- `market_category_registry.py` / `market_router.py`: fail-closed market classification and the original BTC strategy router. Non-BTC categories are never sent to the BTC strategy. The separate `market_decision_pipeline.py` can record isolated research-paper simulations only after a category model/evidence provider and strict risk gates pass.
- `trading_mode.py`: fail-closed dashboard mode gate and live-readiness checklist. The real option requests a readiness check; this build cannot enable live execution because it has no order-submission adapter.
- `multi_user_security.py`: password/session security primitives and readiness checklist; no login middleware, tenant-scoped journal, or public account signup is enabled.
- `payload_safety.py`: recursive sensitive-key removal for untrusted webhook payloads.
- `market_decision_engine.py` / `market_analysis_provider.py` / `market_decision_pipeline.py`: separately evaluate ranked cross-market candidates against a configured probability/evidence provider, current book, liquidity, spread, estimated fees/slippage, validation quality and paper-only exposure limits. Without a configured provider, no cross-market edge or paper entry is created. This pipeline has no live-order endpoint.
- `dashboard.html`: current-window state, preserved latest directional evaluation, active-position entry rationale, trend and decision detail, journal, analytics UI, and a separate market research/paper-results panel.

## Render configuration

Use the same `DATABASE_URL` on the Web Service and Background Worker so the webhook queue, worker, trade ledger and dashboard share the same PostgreSQL database. Keep `PAPER_WORKER_ENABLED=false` on the Web Service when using a separate Background Worker; the worker runs independently with the same shared DB.

Required webhook variable on the Web Service:

```text
GAINZALGO_WEBHOOK_SECRET=<your existing secret; do not put it in GitHub>
```

The webhook accepts a matching top-level `secret` or `token`, strips credential-like keys recursively before storing payloads, and rejects bodies larger than `GAINZALGO_WEBHOOK_MAX_BODY_BYTES` (default 16 KiB; maximum configurable limit 64 KiB).

Manual APIs that write to the journal (`POST /evaluate`, `POST /paper/open`, `POST /paper/{trade_id}/settle`) are disabled unless `PAPER_ADMIN_API_SECRET` is explicitly configured. When enabled, pass it in the `X-Paper-Admin-Secret` request header. This secret is unrelated to `GAINZALGO_WEBHOOK_SECRET`. The automated paper worker and signed webhook do not need it. Read-only routes do not require this header; for private performance data, keep the service private or add authentication at the hosting/proxy layer.

## APIs

- `GET /` — dashboard UI.
- `GET /health`, `GET /status` — health/runtime status.
- `GET /api/dashboard` — heartbeat, separate current-window status, latest directional evaluation, linked active-position entry context, trend monitor, risk and paper-trade journal.
- `GET /api/analytics` — actual paper performance cohorts, rolling results, exit-shadow review and separately labeled rejected-signal counterfactuals.
- `GET /api/learning` — persistent post-trade outcome feedback, entry-feature snapshots, cohort summaries, research notes, and the active bounded adaptive-entry policy. The endpoint is read-only; the worker applies the policy after settled outcomes are journaled.
- `GET /api/decision-audit` — recent audit events, read-only.
- `GET /api/category-readiness` — research readiness profiles; this endpoint never enables or routes trades.
- `POST /api/market-scan` / `GET /api/market-scan` — starts/resumes and reports progress for the background paginated open-market inventory. It follows cursor pages up to the configured safety caps (500 pages / 100,000 distinct markets by default), refreshes every 10 minutes when enabled, displays up to 50 examples, and reports partial/failure states explicitly. Inventory alone does not submit trades.
- `GET /api/market-analysis` — research provider status, last batch decisions, blockers, and isolated research-paper performance.
- `POST /api/market-analysis/run` — runs an admin-session-protected research batch. It may create only separate simulated paper entries after the external model/evidence provider, market-price, liquidity, edge, and exposure checks pass. It never sends live orders.
- `GET /api/trading-mode`, `POST /api/trading-mode` — show the effective paper-only state or reject a request for real mode; the browser cannot modify deployment settings.
- `GET /api/live-readiness`, `GET /api/multi-user-readiness` — checklists for unimplemented live-order and production multi-user requirements; neither endpoint enables these capabilities.
- `GET /diagnostics/market-data` — cached public-market diagnostic; it never places or modifies trades.
- `POST /webhooks/gainzalgo` — authenticated alert intake only; recording an alert does not place a trade.
- `GET /api/alerts/recent`, `GET /journal/recent`, `GET /paper/trades`, `GET /paper/stats` — read-only signal/trade inspection.
- `GET /markets/btc-candidates`, `GET /markets/{ticker}`, `GET /markets/{ticker}/orderbook` — public Kalshi information.
- `POST /evaluate`, `POST /paper/open`, `POST /paper/{trade_id}/settle` — manual journal/evaluation routes; require `PAPER_ADMIN_API_SECRET` and the `X-Paper-Admin-Secret` header.

`/api/category-readiness` remains the readiness view for routing categories into the original BTC strategy; non-BTC markets must not be passed into that strategy. The separate research pipeline can run isolated hold-to-settlement paper experiments for allowlisted categories only when `MARKET_ANALYSIS_PROVIDER_URL` is configured and its model/evidence claims pass validation. Provider-service source is included as `market_analysis_provider_service.py`, but it must be deployed as a separate HTTPS service and configured with your own OpenAI API key. By default the scanner inventories all available pages up to its caps, while deeper research rotates through the top 500 activity-ranked candidates and analyzes up to 10 per batch; this is not a guarantee that every market receives deep analysis in each cycle.

## Automated multi-market research and win-rate evaluation

The scanner inventories standard and multivariate open-event markets within hard caps. The separate, paper-only research pipeline rotates through an activity-ranked pool (500 by default) and analyzes up to 10 per 300-second cycle; it does not deeply research every contract simultaneously. It needs a vetted HTTPS `MARKET_ANALYSIS_PROVIDER_URL` that returns category-appropriate calibrated probabilities and source-attributed current evidence. After provider and staging validation, set `MARKET_ANALYSIS_AUTO_START=true`; only after reviewing results should `MARKET_AUTOPAPER_ENABLED=true` be considered. Keep `TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false`. Without a provider or passing independent calibration, the pipeline refuses paper entries rather than inventing a probability.

The reported 95-trade / 56% win-rate sample is not large enough to tune thresholds confidently. The analytics response includes a Wilson confidence interval plus average net P&L, profit factor and drawdown. Prefer changes that improve fee-adjusted out-of-sample expectancy and calibration, not just the headline win percentage; increasing win rate by taking low-payout/high-price contracts can worsen overall profitability.

## Local install and test

Python 3.11+ is recommended.

```bash
python -m venv .venv
# macOS/Linux: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt pytest pyyaml
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

The test workflow in `.github/workflows/python-app.yml` compiles the code, runs unit/integration modules, exercises the shared webhook-to-worker paper flow using offline provider stubs, and tests category readiness and API safety. Run `python -m pytest -q`, `python smoke_shared_alert_flow.py`, and `python stress_phase456_readiness.py` locally; the stress script is deterministic and offline, does not use credentials, and never places orders.

## Operational guidance

1. Commit to a branch or create a backup before replacing production files.
2. Wait for all GitHub Actions steps to pass. Do not deploy if any test fails.
3. Deploy the same commit to the Render Web Service and Background Worker.
4. Verify worker heartbeat, `btc_trend_monitor.status`, separate current-window status, latest directional evaluation, active-position entry rationale, `/api/analytics`, `/api/decision-audit`, and `/api/category-readiness`.
5. Observe a sufficiently large out-of-sample paper sample before changing strategy thresholds. Review drawdown and fee-adjusted net P&L, not only win rate.
6. Confirm `/api/learning` records each newly settled trade once and keeps hypothetical rejected-signal/exit-shadow results separate from actual results. Keep manual write endpoints protected and never expose secrets in GitHub, screenshots, logs or returned payloads.

The dashboard has a Paper/Real mode selector, but the Real option is deliberately locked and only displays readiness blockers; it cannot enable or place orders. Before a real-order adapter could be considered, it must be built and tested against Kalshi Demo with separate demo credentials, idempotent client order IDs, fill/position reconciliation, exact market fees, slippage checks, concurrency-safe limits, authenticated private APIs, and an emergency cancel/reconcile procedure. Historical results, simulated exits, market-implied odds, internal scores and rejected-signal counterfactuals are descriptive evidence, not guarantees of future returns or executable prices.

## Major-drop profit protection (paper-only)

After a paper position has reached and armed its fee-adjusted take-profit target, optional profit protection can trigger an immediate simulated exit when either (a) the internal engine detects a predefined major opposing setup, or (b) a newly closed 1-minute BTC candle moves adversely by at least `PAPER_TAKE_PROFIT_MAJOR_MOVE_ATR_MULTIPLIER` times the ATR baseline from prior closed candles. Configure `PAPER_TAKE_PROFIT_PROFIT_PROTECTION_ENABLED=true` and `PAPER_TAKE_PROFIT_MAJOR_MOVE_ATR_MULTIPLIER=2.5`. The exit is modeled from the observed Kalshi bid, subtracts entry and exit fees, and is logged as `PROFIT_PROTECTION`. This is best-effort only: gaps, wide spreads, missing liquidity, or polling delays can result in an exit below break-even; no no-loss outcome is guaranteed. It applies only to simulated paper positions and never enables real orders.

### Entry-price semantics update

`PAPER_ENTRY_TARGET_MAX_CENTS` (default 80¢) is a soft preferred price, not a trade blocker. A directional signal that passes freshness, major-flaw, quote, spread, risk, and budget checks may enter above that preference up to `PAPER_MAX_ENTRY_CENTS` (default hard cap 92¢). Quotes above the hard cap wait and are not opened. Higher entry prices leave less maximum upside; a direction/evidence score is not a calibrated win probability.


## Phases 1–3 context upgrade

See `README_PHASES_1_TO_3_COMPLETION.md` for the latest reconciled work: UTC weekday/weekend and time-of-day cohorts, descriptive trend/range and existing volatility-warning cohorts, chronological holdout review, append-only adaptive-target change history, and current-alert dashboard stabilization. Signal weights remain fixed pending sufficient out-of-sample evidence. The bot remains paper-only.


### Automated market research and validation status

The cross-market pipeline is a guarded research-to-paper-decision framework. Deep analysis requires a separately configured HTTPS `MARKET_ANALYSIS_PROVIDER_URL` that supplies calibrated probabilities and source-attributed, current evidence. Without it, the pipeline fails closed and does not fabricate a probability or create research-paper positions. This snapshot contains a low-level signed V2 order adapter for isolated Demo testing, but it is not wired into the application live-execution path. Keep `TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false`.

The source includes `run_validation_soak.py`, which accumulates actual offline test-command runtime and defaults to 7,200 seconds (two hours). The latest 215-test suite and stress/smoke tests passed, but the requested two-hour soak has not been completed in this environment. See `PHASE456_VALIDATION.md` for exact status.


## All-phases deployment quickstart

Use [`BTC_Bot_All_Phases_Environment_Quickstart.md`](BTC_Bot_All_Phases_Environment_Quickstart.md) for the consolidated Render environment settings and deployment order. See [`BTC_BOT_MARKET_PIPELINE_VALIDATION.md`](BTC_BOT_MARKET_PIPELINE_VALIDATION.md) for the latest known local validation and explicit limitations.


## Optional market-analysis provider service

For multi-market research, deploy `market_analysis_provider_service.py` as a **separate** Render Web Service with start command:

```bash
uvicorn market_analysis_provider_service:app --host 0.0.0.0 --port $PORT
```

Set `OPENAI_API_KEY` (created in your own OpenAI Platform project), `OPENAI_MODEL` (a model available to your account that supports the Responses API web-search and strict structured outputs), `MARKET_ANALYSIS_PROVIDER_API_KEY` (same generated shared token as the main service), and the same `DATABASE_URL` on that service. On the main Web Service and Background Worker, set `MARKET_ANALYSIS_PROVIDER_URL=https://<provider-service>.onrender.com/analyze` and the same provider token. The provider can search for current news and evidence, but it does not place orders.

Model probabilities are stored with decision timestamps and compared with actual market resolution outcomes when available. A model/category stays ineligible until at least 100 prior predictions have subsequently resolved, both YES and NO outcomes have sufficient representation, and the observed Brier score meets the configured threshold. This intentionally causes a calibration warm-up; do not fake or manually invent a validation report.

The protected endpoint `GET /api/diagnostics/replay-export` exports curated historical trade and decision CSVs after dashboard admin login. It excludes credentials and private keys. Use `python audit_historical_export.py btc_bot_historical_replay_export.zip` to audit the real recorded journal, including win-rate confidence intervals, net P&L, entry-price/source cohorts, counterfactual records, and model Brier scores. It is an audit of logged decisions, not a complete counterfactual replay of markets for which contemporaneous quotes/news/model inputs were never stored.


**Live order status:** `kalshi_order_adapter.py` is a low-level signed V2 adapter with Demo-only default and mocked tests. It is not registered as a live execution adapter in `trading_mode.py`; the app-level Real switch intentionally remains blocked until Demo lifecycle testing and reconciliation are complete. Keep `TRADING_MODE=paper`, `LIVE_TRADING_ENABLED=false`, `LIVE_TRADING_APPROVED=false`, and `MARKET_LIVE_EXECUTION_ENABLED=false`.


### Kalshi scanner recovery and diagnostics

The scanner inventories both Kalshi's standard event feed and the separate multivariate/combo event feed using feed-prefixed cursors. Event categories from the parent event override inconsistent market-row category fields. It uses bounded retries and a conservative public-data request cadence. On failure, the dashboard reports the upstream HTTP status and failed page number without exposing opaque cursor values. Transient 408/425/429/5xx/network failures are retried; a rejected continuation cursor triggers one clean restart, and persistent failures resume from the last safe cursor while the dashboard autopilot is enabled. Public market inventory does not require a Kalshi private key. If Render still reports a failure, run `python diagnose_kalshi_catalog.py --pages 5` in the Web Service Shell to test both public endpoints safely.


## Market scanner and automatic research paper-entry behavior (2026-10-24)

- Inventory requests page through the official `/events` feed and the separate `/events/multivariate` feed, including nested markets and official parent-event category metadata. The latter feed matters for combo markets not present in the standard listing.
- Upstream status/page diagnostics are exposed without logging cursor values. Transient failures are retried; a rejected cursor causes one clean restart; the dashboard autopilot scheduler retries partial or failed inventories.
- The scanner is inventory-only. With the secure dashboard Autopilot switch ON, a separately configured HTTPS research provider and the deployment paper gate enabled, a rotating activity-ranked subset is analyzed and may create *separate research paper records* only after liquidity, spread, fee/slippage, net-edge, evidence freshness, exact ticker, category allowlist, model calibration and risk gates pass.
- Default deep-research budget is 500 ranked research candidates, 10 candidate analyses per 300-second cycle. A full rotation takes roughly 250 minutes at defaults; this avoids uncontrolled provider costs and rate-limit spikes. You can tune the batch/interval after staging evidence.
- Unknown categories, BTC 15-minute markets, stale/missing evidence and uncalibrated predictions do not enter this research-paper path. The original BTC strategy remains separate.
- No live orders are enabled by this update.
- Run `python diagnose_kalshi_catalog.py --pages 5` in a Render Web Service Shell for a read-only check of standard and multivariate endpoints; it prints HTTP status, page shape, counts and cursor length but never the cursor value.

## Current scanner/autopilot fix (2026-10-24)

The current Kalshi order-book parser supports the documented `orderbook_fp.yes_dollars/no_dollars` schema as well as legacy cent-format fixtures. The provider readiness gate requires both an HTTPS `MARKET_ANALYSIS_PROVIDER_URL` and a non-empty `MARKET_ANALYSIS_PROVIDER_API_KEY`. The scanner retains inventory diagnostics for non-open statuses but only places explicitly open/active research candidates into the analysis pool. Use `python diagnose_kalshi_catalog.py --pages 5` to safely test several continuation pages from both public catalog feeds after deploying.

The latest local suite is 233 passing tests; see `BTC_BOT_MARKET_PIPELINE_VALIDATION.md` for the current matrix and limitations. The actual two-hour soak, Render deployment verification after this patch, and the user’s production-journal replay are still pending.



## Paper exit protection and research-category reporting (2026-10-24)

- The paper worker supports a fee-aware take-profit with a trailing floor. Example worker settings are `PAPER_TAKE_PROFIT_ENABLED=true`, `PAPER_TAKE_PROFIT_PERCENT=10`, and `PAPER_TAKE_PROFIT_TRAIL_PERCENT=5`. The requested percentage is measured against entry cost including entry fees and accounts for estimated exit fees. A target above Kalshi's 100¢ maximum bid is capped at 100¢ and emits a warning instead of remaining unreachable; a capped target may not attain the requested ROI.
- The paper-only loss-streak circuit breaker pauses new BTC entries after four consecutive negative-net settled trades for a configurable cooldown (`PAPER_CONSECUTIVE_LOSS_LIMIT=4`, `PAPER_LOSS_STREAK_COOLDOWN_MINUTES=60`). Existing open positions are still managed before the entry gate is evaluated. Set the limit or cooldown to `0` to disable the guard.
- The separate research journal reports performance by `NFL`, `NBA`, `WNBA`, `NHL`, `MLB`, `NCAA Sports`, `Other Sports`, `Politics`, `Crypto Events`, `Economics`, `Weather`, `Financial Markets`, `Entertainment`, `Companies`, `World Events`, and `Science & Tech`. League labels are reporting-only; they do not bypass the generic sports evidence and model-calibration checks. The research journal never changes BTC strategy win-rate metrics.
- These category rows show zero/unknown when no actual research paper trades have been recorded. Current research automation remains fail-closed until an operator-provided HTTPS analysis provider is configured and a model/category has at least 100 subsequently resolved forecasts, balanced YES/NO outcomes, and Brier score at or below 0.22. Do not seed fake predictions to force the gate open.
