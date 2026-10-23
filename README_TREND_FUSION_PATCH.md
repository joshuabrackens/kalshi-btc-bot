# BTC trend-to-signal integration patch (paper mode only)

This follow-up binds the existing, continuously refreshed `btc_trend_monitor` snapshot into the TradingView-primary **hybrid paper-entry** decision. The worker passes the snapshot from the same cycle to `evaluate_fusion`; it is no longer dashboard-only.

## Replace in the repository root

- `paper_worker.py`
- `signal_fusion.py`

## Add

- `test_signal_fusion_trend_gate.py`

Do not replace any other files for this focused patch. Keep these existing Render settings:

- `TRADING_MODE=paper`
- `LIVE_TRADING_ENABLED=false`
- `PAPER_MAX_ENTRY_CENTS=92`
- `PAPER_ENTRY_TARGET_MAX_CENTS=80`
- `PAPER_WORKER_INTERVAL_SECONDS=15`
- `BTC_TREND_MONITOR_INTERVAL_SECONDS=30`
- `BTC_TREND_MONITOR_MAX_CACHE_AGE_SECONDS=45`
- `MAX_TRADE_DOLLARS=1.00`
- `MAX_DAILY_LOSS_DOLLARS=10.00`

## How the new gate works

1. For hybrid entries, the trend snapshot must have `status=ok` and be recent (using `BTC_TREND_MONITOR_MAX_CACHE_AGE_SECONDS`). Missing, degraded, or stale snapshot data fails closed and the trade is not opened.
2. A strong opposing chart bias vetoes the proposed direction only when all criteria pass: opposite `candidate_bias`, evidence score at or above `PAPER_HYBRID_TREND_VETO_MIN_SCORE` (default 65), opposing evidence point margin at or above `PAPER_HYBRID_TREND_VETO_MIN_MARGIN` (default 4), and at least 2 of the 1m/5m/15m trend labels aligned against the alert.
3. Mixed or weak opposing trend evidence is logged but does not automatically veto a fresh TradingView alert.
4. Existing alert freshness, current 15-minute window, order-book validity, spread, 80-cent target, 92-cent hard cap, open-position rule, daily risk limit, and simulation-only guard still apply. The evidence score is not a calibrated probability.

Optional environment tuning (defaults are applied when unset):

- `PAPER_HYBRID_TREND_VETO_MIN_SCORE=65`
- `PAPER_HYBRID_TREND_VETO_MIN_MARGIN=4`

These values should not be loosened based on a handful of trades. This patch does not enable live trading.

## Validation before/after deploy

- Run `python -m unittest test_signal_fusion_trend_gate.py`.
- Run the project's other tests appropriate to the current environment.
- Deploy the same commit to both Render Web Service and Background Worker.
- Confirm the worker starts with `live_trading=False` and the latest hybrid signal payload contains `chart_trend_at_decision`, including the bias/evidence/timeframe snapshot and the fusion reason.
- Leave the bot in paper mode during verification.
