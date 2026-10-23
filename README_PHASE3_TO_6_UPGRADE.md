# BTC Bot Phase 3–6 Improvement Package (paper-only)

This package extends the existing BTC-only paper bot. It does not enable real orders and does not relax entry, spread, freshness, exposure, or daily-loss controls.

## What this upgrade adds

### Phase 3 — Complete the decision journal
- Adds a `decision_audit` table for webhook outcomes, alert status changes, signal evaluations, simulated entries/exits, settlements, and completed counterfactual reviews.
- Adds normalized decision metadata to `signal_log`: proposed direction, decision status, observed entry ask/spread, trend bias/evidence, trend snapshot age, and counterfactual review fields.
- Links audit events to alert IDs, signal-log IDs, and trade IDs wherever those IDs exist.
- Records later risk-gate changes (for example, a candidate becoming `blocked`) rather than leaving its earlier status as if it had traded.

### Phase 4 — Performance and entry-quality measurement
- Adds read-only `/api/analytics` endpoint.
- Reports settled journal results: net P&L, win rate, average net P&L per settled trade, profit factor, peak-to-trough drawdown, and rolling last-20/last-50 summaries.
- Segments actual trade results by entry-price bucket, recorded chart-trend alignment, and chart evidence-score range.
- Makes clear that evidence scores and ask-price percentages are not calibrated win probabilities.

### Phase 5 — Exit review
- Compares stored trend-stop shadow observations with the actual journal P&L for the same settled trades.
- Shows counts, hypothetical shadow-exit P&L, actual P&L, and the difference, with a warning that an observed bid is not a guaranteed fill.

### Phase 6 — Dashboard and operational auditability
- Adds the Performance / Decision Quality section to the dashboard.
- Shows recent decision events, alert status transitions, counterfactual results, and operational note counts.
- Adds read-only `/api/decision-audit` endpoint for troubleshooting and record review.
- Tracks official settlement and manually recorded paper entry/settlement events.

### Rejected-signal counterfactual review
For no-entry decisions that have a valid recorded side ask and validated market ticker, the worker periodically checks the official market result after the expected close. Each is evaluated as **one hypothetical contract held to settlement**, subtracting the recorded entry fee. This is shown separately from actual P&L. It deliberately excludes stops/exits, exit fees, slippage, and market impact; it must not be treated as a trade or a guaranteed executable return.

The lookup happens only **after** the current worker decision cycle and at most one public result lookup per idle/monitoring cycle so counterfactual review does not precede a fresh signal decision.

## Files to replace
At the repository root, replace:
- `db_store.py`
- `main.py`
- `paper_worker.py`
- `dashboard.html`
- `signal_fusion.py`
- `.github/workflows/python-app.yml`

## Files to add
At the repository root, add:
- `performance_analytics.py`
- `test_decision_audit.py`
- `test_performance_analytics.py`
- `test_analytics_api.py`
- `test_counterfactual_audit.py`
- `README_PHASE3_TO_6_UPGRADE.md`

Keep your existing `test_paper_worker.py`, `test_trend_stop.py`, `test_signal_fusion_trend_gate.py`, `btc_trend_monitor.py`, `signal_engine.py`, `kalshi_api.py`, `candle_feed.py`, `trend_stop.py`, `market_discovery.py`, dependency files, and Dockerfile. The updated workflow runs the existing worker tests, the seven trend-fusion tests, the existing signal/market tests, and the new analytics tests in separate steps to avoid test stubs leaking between test modules.

## Deploy steps
1. Create a GitHub branch or backup before replacing files.
2. Apply the six replacements and six additions listed above (the new tests are necessary for the updated workflow).
3. Commit with message `Add decision audit and retrospective strategy analytics`.
4. Wait for GitHub Actions to finish. **Do not proceed if a test step is red.**
5. Let both Render Web Service and Background Worker deploy the same commit.
6. Verify `/api/analytics`, `/api/decision-audit`, and the dashboard after deployment. Database migrations run automatically from `ensure_schema`; no manual SQL migration is required.

## Render settings
No new environment variables are required. Preserve:
- `TRADING_MODE=paper`
- `LIVE_TRADING_ENABLED=false`
- `PAPER_MAX_ENTRY_CENTS=92` (existing hard cap)
- `PAPER_ENTRY_TARGET_MAX_CENTS=80` (preferred entry target)
- `MAX_TRADE_DOLLARS=1.00`
- `MAX_DAILY_LOSS_DOLLARS=10.00`
- `PAPER_WORKER_INTERVAL_SECONDS=15` (keep your current setting)

Keep the same `DATABASE_URL` on the Web Service and Background Worker so the dashboard, worker journal, and audit log share a database.

## Validation performed in the local working copy
- All five modified/new runtime Python modules compile.
- Seven trend-fusion unit tests pass.
- Existing available tests passed for the trend monitor, signal engine, entry-quality gate, order-book pricing, current signal/window behavior, wait alerts, market-data diagnostics, trend-monitor persistence, Kalshi API retry behavior, and the pre-existing strategy performance audit.
- New audit schema/persistence, analytics aggregation/API, and counterfactual-settlement tests pass.
- The locally mounted file bundle did not include `test_paper_worker.py`, `test_trend_stop.py`, `candle_feed.py`, `trend_stop.py`, or `market_discovery.py`; therefore those project-specific worker tests cannot be re-run in this container snapshot. The included GitHub Actions workflow runs the worker and trend-stop tests in separate processes, and also runs the local Kalshi API tests. All GitHub Actions steps must pass after your commit.

## Safety / interpretation
- This remains a paper-only system; no real-order placement path was added.
- Counterfactual results are not actual trades and are not combined with actual paper P&L.
- Historical results and small samples do not establish a profitable edge.
- The bot's evidence score is not a win probability. Do not enable live trading as part of this upgrade.
