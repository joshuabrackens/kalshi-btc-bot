# Phase 3–7 BTC bot master upgrade (paper-only)

This repository includes the existing BTC-only hybrid paper-trading flow plus retrospective strategy analytics, safer webhook payload handling, protected manual write APIs, a category-readiness registry, and a full GitHub Actions test matrix. This upgrade does not add real order placement or relax risk controls.

## Improvements

### Phase 3 — Decision audit
- Appends `decision_audit` events for webhooks, alert status transitions, evaluations, paper openings/exits/settlements, and completed counterfactual reviews.
- Stores normalized proposed direction, decision status/reason, entry ask/spread, trend bias/evidence and freshness, with alert/signal/trade references where available.
- Records pending market/order-book states without falsely consuming a fresh alert, and preserves alert freshness/window safeguards.

### Phase 4 — Historical performance analytics
- Adds read-only `/api/analytics`: actual net P&L, win rate, average net per settled trade, profit factor, drawdown, rolling last-20/last-50, entry-price cohorts, trend-alignment cohorts, evidence-score cohorts, and decision-status totals.
- Adds `/api/learning` backed by an idempotent `trade_learning` table, which records one snapshot per settled paper trade and compares outcome cohorts across entry price, chart trend, evidence score, volume, and momentum. The worker updates this after settlement and backfills older settled trades in bounded batches.
- Pulls the most recent journal rows when applying the 10,000-row analysis limit.
- The analytics are descriptive. Scores/asks are not calibrated probabilities; small samples are not evidence of edge.

### Phase 5 — Exit and rejected-signal retrospective review
- Compares actual settled P&L with recorded trend-stop shadow observations for the same trades.
- Checks eligible rejected/no-entry signals against official Kalshi settlement as one hypothetical contract held to settlement, including the recorded entry fee. These results are separate from real paper-trade counts/P&L and exclude exit mechanics, slippage and impact.
- Counterfactual review executes after the worker's trade-decision cycle and is bounded to one public result lookup per idle/monitoring cycle.

### Phase 6 — Observability, security and operations
- Adds read-only `/api/decision-audit`, `/api/analytics`, `/api/learning`, `/api/category-readiness`, market-data diagnostics, and worker-heartbeat status.
- Keeps market summary asks out of executable pricing; asks are derived from the corresponding current opposite-side order-book bid.
- Fixes WAIT signal score journaling so the actual evidence score is preserved even though the decision remains WAIT.
- Prevents webhook body-size abuse (16 KiB default, configurable up to 64 KiB) and removes sensitive key/value fields recursively before saving untrusted webhook payloads.
- Disables manual journal/evaluation write APIs unless `PAPER_ADMIN_API_SECRET` is explicitly configured, and requires `X-Paper-Admin-Secret` on those routes. The automatic worker and signed webhook don't need that admin secret.
- Migrates app startup to FastAPI's lifespan mechanism.

### Phase 7 — Multi-market readiness foundation (not active trading)
- Adds `market_category_registry.py` and `/api/category-readiness` to document category-specific prerequisites.
- BTC 15-minute is the only paper-enabled profile. Sports, politics/elections, economics, weather, crypto-event, other financial-market and entertainment profiles remain research-only. They require independent data sources, resolution/settlement rules, probability calibration and tests; none is routed through the BTC strategy.

## Environment settings

Keep the existing settings on Render:
- `TRADING_MODE=paper`
- `LIVE_TRADING_ENABLED=false`
- `PAPER_MAX_ENTRY_CENTS=92`
- `PAPER_ENTRY_TARGET_MAX_CENTS=80`
- `MAX_TRADE_DOLLARS=1.00`
- `MAX_DAILY_LOSS_DOLLARS=10.00`
- `PAPER_WORKER_INTERVAL_SECONDS=15`
- `BTC_TREND_MONITOR_INTERVAL_SECONDS=30`
- `BTC_TREND_MONITOR_MAX_CACHE_AGE_SECONDS=45`

Keep the same `DATABASE_URL` on the Render Web Service and Background Worker. Keep `PAPER_WORKER_ENABLED=false` on the Web Service if using a separate Background Worker. Keep `GAINZALGO_WEBHOOK_SECRET` secret on the Web Service. Only set `PAPER_ADMIN_API_SECRET` if manual `/evaluate` or `/paper/*` writes are deliberately needed; otherwise they remain disabled.

## Validation

Run `python -m compileall -q .`, then the tests listed in `.github/workflows/python-app.yml`. The workflow runs each test module in an isolated process to avoid import stubs leaking between test modules. It also runs `smoke_shared_alert_flow.py`, which uses temporary local storage and deterministic provider stubs rather than network calls.

The current local validation includes paper worker/risk, trend stops, trend fusion, trend monitoring/persistence, signal engine, Kalshi retries, order-book quote pricing, entry guard, signal/window freshness, webhook WAIT handling, diagnostics, strategy audit, decision audit, analytics, counterfactual review, payload redaction/body limits, manual API auth and category readiness. All available isolated modules and the offline end-to-end smoke test should pass before deployment.

## Deploy

1. Create a branch or backup.
2. Apply the modified/added files from the master ZIP; never commit `.env` or a local database. `.gitignore` excludes local SQLite journals and Python caches.
3. Commit to a branch or `main`, then wait for every GitHub Actions step to pass.
4. Let both Render services deploy the same commit.
5. Verify `/health`, `/api/dashboard`, `/api/analytics`, `/api/learning`, `/api/decision-audit`, `/api/category-readiness`, and `/diagnostics/market-data`.

No manual SQL migration is required; `ensure_schema()` applies the schema changes. No new database or API credentials are included.

## Post-trade learning boundary

Every settled paper trade is recorded once with an entry-time feature snapshot and a realized P&L label. The report supplies per-trade observations and sample-gated cohort comparisons; it does not automatically alter strategy parameters after each result. Any proposed rule change must be reviewed, regression-tested, and validated on an out-of-sample paper sample. Counterfactual rejected-signal results and shadow exits remain separate from actual trade outcomes.

## Interpretation and safety

- Still paper only; no real-order placement path was added.
- Counterfactuals and shadow exits are hypothetical, not actual trade returns or guaranteed fills.
- Manual write APIs are disabled by default and should not be opened without a strong secret.
- Market category readiness is descriptive scaffolding, not a claim that the bot can trade those categories.
- Do not enable live trading as part of this upgrade.
