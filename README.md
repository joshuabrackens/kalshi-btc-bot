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

The 92¢ value is the hard cap; 80¢ is the lower preferred entry target. The worker does not chase an ask above the active target.

## Architecture

- `main.py`: FastAPI web service, signed TradingView/GainzAlgo webhook, dashboard APIs, paper journal endpoints, diagnostics and analytics.
- `paper_worker.py`: paper-only worker, fresh-alert validation, order-book pricing, trend fusion, entry/risk checks, exits, official settlement and retrospective rejected-signal review.
- `candle_feed.py`: public Binance closed-candle data.
- `btc_trend_monitor.py`: multi-timeframe chart evidence (trend/momentum/volume/structure/volatility); it is diagnostic evidence, not a probability estimate.
- `signal_engine.py` / `signal_fusion.py`: internal strategy diagnostics and TradingView-primary hybrid gate.
- `kalshi_api.py` / `market_discovery.py`: public Kalshi market discovery, current order-book quotes and market results.
- `db_store.py`: shared SQLite/PostgreSQL adapter and schema migrations.
- `performance_analytics.py`: descriptive historical P&L and decision cohort analysis.
- `trade_learning.py`: idempotent per-settled-trade feature snapshots, outcome reviews, and sample-gated cohort insights; it recovers legacy entries by external alert ID and never automatically changes strategy rules.
- `market_category_registry.py`: Phase 7 readiness scaffold. BTC 15m is the only paper-enabled profile; all non-BTC categories are research-only.
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
- `GET /api/learning` — persistent post-trade outcome feedback, entry-feature snapshots, cohort summaries, and research notes. The worker backfills settled journal rows idempotently; the endpoint is read-only and never tunes rules or alters trade permissions.
- `GET /api/decision-audit` — recent audit events, read-only.
- `GET /api/category-readiness` — research readiness profiles; this endpoint never enables or routes trades.
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

The test workflow in `.github/workflows/python-app.yml` compiles the code, runs each unit/integration module in an isolated process, exercises the shared webhook-to-worker paper flow using offline provider stubs, and tests category-readiness and API safety. Run the smoke test directly with `python smoke_shared_alert_flow.py`; it uses a temporary database and deterministic stubs only.

## Operational guidance

1. Commit to a branch or create a backup before replacing production files.
2. Wait for all GitHub Actions steps to pass. Do not deploy if any test fails.
3. Deploy the same commit to the Render Web Service and Background Worker.
4. Verify worker heartbeat, `btc_trend_monitor.status`, separate current-window status, latest directional evaluation, active-position entry rationale, `/api/analytics`, `/api/decision-audit`, and `/api/category-readiness`.
5. Observe a sufficiently large out-of-sample paper sample before changing strategy thresholds. Review drawdown and fee-adjusted net P&L, not only win rate.
6. Confirm `/api/learning` records each newly settled trade once and keeps hypothetical rejected-signal/exit-shadow results separate from actual results. Keep manual write endpoints protected and never expose secrets in GitHub, screenshots, logs or returned payloads.

Do not enable live trading as part of this project. Historical results, simulated exits, market-implied odds, internal scores and rejected-signal counterfactuals are descriptive evidence, not guarantees of future returns or executable prices.
