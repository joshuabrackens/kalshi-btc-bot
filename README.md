# Kalshi BTC 15-Minute Paper Bot

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
- `market_category_registry.py` / `market_router.py`: Phase 4 market-catalog classification and bounded scan. BTC 15m is the only paper-enabled profile; all non-BTC categories are research-only and the scanner never executes orders.
- `trading_mode.py`: fail-closed dashboard mode gate and live-readiness checklist. The real option requests a readiness check; this build cannot enable live execution because it has no order-submission adapter.
- `multi_user_security.py`: password/session security primitives and readiness checklist; no login middleware, tenant-scoped journal, or public account signup is enabled.
- `payload_safety.py`: recursive sensitive-key removal for untrusted webhook payloads.
- `dashboard.html`: current-window state, preserved latest directional evaluation, active-position entry rationale, trend and decision detail, journal, and analytics UI.

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
- `GET /api/market-scan` — manually invoked, cached (30-second default; `MARKET_SCAN_CACHE_TTL_SECONDS` is optional) and bounded public-market inventory; concurrent/repeated dashboard clicks do not repeatedly call the upstream API during the cache window. Non-BTC and unknown markets cannot reach the BTC strategy. The scanner follows at most five 100-market pages (500 total), is cached for 30 seconds by default, and does not submit trades.
- `GET /api/trading-mode`, `POST /api/trading-mode` — show the effective paper-only state or reject a request for real mode; the browser cannot modify deployment settings.
- `GET /api/live-readiness`, `GET /api/multi-user-readiness` — checklists for unimplemented live-order and production multi-user requirements; neither endpoint enables these capabilities.
- `GET /diagnostics/market-data` — cached public-market diagnostic; it never places or modifies trades.
- `POST /webhooks/gainzalgo` — authenticated alert intake only; recording an alert does not place a trade.
- `GET /api/alerts/recent`, `GET /journal/recent`, `GET /paper/trades`, `GET /paper/stats` — read-only signal/trade inspection.
- `GET /markets/btc-candidates`, `GET /markets/{ticker}`, `GET /markets/{ticker}/orderbook` — public Kalshi information.
- `POST /evaluate`, `POST /paper/open`, `POST /paper/{trade_id}/settle` — manual journal/evaluation routes; require `PAPER_ADMIN_API_SECRET` and the `X-Paper-Admin-Secret` header.

`/api/category-readiness` describes a safe expansion roadmap, not an active multi-market bot. Sports, politics/elections, economics, weather, crypto-event and other financial/event categories stay disabled until separate primary data sources, settlement validators, calibrated models and tests exist. Non-BTC markets must not be passed into the BTC strategy.

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
