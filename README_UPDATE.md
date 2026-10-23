# BTC bot dashboard order + settled-trade learning update

## Included

- `dashboard.html`: puts Realized P&L, Engine connection, and Risk limits in the first row, in that order. Adds a readable Learning system panel and keeps strategy diagnostics human-readable.
- `main.py`: API returns learning statistics and the latest signal's learning adjustment; starts the embedded worker when `PAPER_WORKER_ENABLED=true` and logs the launch.
- `paper_worker.py`: integrates learn-from-settled-trade hooks with the current TradingView-primary hybrid worker and trailing take-profit/trend-stop logic.
- `learning_system.py`: persistent, transparent, conservative outcome learner.
- Tests and GitHub Actions workflow run worker safety, trend stop, hybrid/take-profit, and learning tests.

## Apply to GitHub

From the extracted bundle, replace these files at the repository root:

- `paper_worker.py`
- `main.py`
- `dashboard.html`
- `trend_stop.py`
- `test_paper_worker.py`
- `test_trend_stop.py`
- `test_hybrid_take_profit.py`

Add these new root files:

- `learning_system.py`
- `test_learning_system.py`

Replace `.github/workflows/python-app.yml` with the included workflow.

Keep your existing `signal_engine.py`, `candle_feed.py`, `market_discovery.py`, `kalshi_api.py`, and `requirements.txt` unless your repo has different/newer versions that these files depend on. Do not upload the ZIP itself into the repository.

Commit and wait for GitHub Actions to pass before relying on the updated code.

## Learning behavior

- Every closed paper trade is recorded once in `trade_learning_events`, and per-context summary profiles are updated.
- Context labels come from saved entry diagnostics (e.g. higher-timeframe agreement/opposition, volume confirmation, box state, and market structure).
- After at least `PAPER_LEARNING_MIN_SAMPLES` examples per context (default 8), a context with net ROI at or below `PAPER_LEARNING_BAD_ROI_PERCENT` (default -8%) is flagged as historically underperforming.
- A learned veto only occurs when at least two independent context dimensions are repeatedly underperforming. One weak factor by itself is a caution, not an automatic veto.
- This is a bounded rule-based adaptive safeguard, not a predictive AI or proof of profitability. It never increases position size, raises daily loss limits, or relaxes spread/entry checks.
- Existing closed trades are backfilled once as legacy outcomes if old entry context was not saved; they are not falsely assigned strategy features.

## Render settings to preserve

Keep these safety settings on any service running the worker:

- `TRADING_MODE=paper`
- `LIVE_TRADING_ENABLED=false`
- `PAPER_WORKER_ENABLED=true` on the Web Service only if you intend the embedded worker to consume the same database as the dashboard
- `PAPER_SIGNAL_SOURCE=hybrid` when using the TradingView-primary indicator
- `PAPER_STOP_LOSS_ENABLED=false` (unless intentionally changed after separate testing)
- `PAPER_TREND_STOP_LOSS_ENABLED=true` only for simulated paper exits
- `PAPER_TREND_STOP_SHADOW_MODE=false` if you intend actual simulated trend-stop exits
- `PAPER_TAKE_PROFIT_ENABLED=true`, `PAPER_TAKE_PROFIT_PERCENT=20`, `PAPER_TAKE_PROFIT_TRAIL_PERCENT=5`, `PAPER_TAKE_PROFIT_REVERSAL_EXIT_ENABLED=true` for the already-configured trailing take-profit behavior
- `PAPER_LEARNING_ENABLED=true`
- `PAPER_LEARNING_MIN_SAMPLES=8`
- `PAPER_LEARNING_BAD_ROI_PERCENT=-8`

Do not suspend the separate Background Worker until you verify the Web Service logs explicitly show `Embedded paper worker thread launched` (and the configured source is hybrid). Once that is confirmed, avoid running two independent consumers/databases; use one worker connected to the dashboard's persistent journal. Render services generally do not share their local SQLite disks automatically.

## Validation performed

Offline command:

`python -m unittest -v test_paper_worker.py test_trend_stop.py test_hybrid_take_profit.py test_learning_system.py`

Result: **31 tests passed**. Python compilation, an API dashboard-data smoke test, and JavaScript syntax check also passed.
