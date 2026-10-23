# BTC Bot Consolidated Paper-Only Upgrade

This package combines the compatible BTC-only improvements prepared in the current conversation:

1. **Order-book correctness:** derive YES/NO asks from the opposite-side best bid, rather than trusting potentially inconsistent market-summary asks.
2. **No-chase entry guard:** keep the existing hard cap and use `PAPER_ENTRY_TARGET_MAX_CENTS=80` as the stricter target. If a fresh alert arrives above the target, wait for a better executable ask only while the alert remains fresh and belongs to the current 15-minute market window. Recheck the order book, spread, hybrid veto, exposure, and other existing checks before a paper entry.
3. **Trade-performance audit:** store a link to the entry signal/evidence score when available, report source-level win/loss/P&L summaries, and avoid inventing historical scores.
4. **Current-window dashboard:** preserve the blue BTC ENGINE design, the countdown until the indicator's expected response, current-signal-only presentation, score/probability separation, trade journal, market-data diagnostics, and risk/worker status.
5. **Continuous BTC trend monitor:** while the worker waits for TradingView, refresh the 1m/5m/15m closed-candle context, RSI/VWAP, volume confirmation, market structure, consolidation state, volatility warning, and evidence totals. Save the latest snapshot to the shared DB and show it on the dashboard. Cache the candle bundle briefly so a fresh alert can reuse preloaded data rather than starting the candle fetch from zero.

## Important semantics

- The trend monitor is **pre-analysis only**. Its candidate bias and evidence score are not a trade approval and are not a calibrated probability.
- A fresh TradingView/GainzAlgo alert remains the directional trigger in hybrid mode. A fresh order book, matching current 15-minute market, alert freshness, entry target, spread, hybrid checks, open-position/risk limits, and all existing guardrails still apply.
- The bot remains paper-only. No real-order route is added.
- The 80-cent target is an initial paper-test setting, not a proven optimal price. A falling price may represent deterioration rather than a bargain.

## Files to replace/add

Replace at the repository root:

- `main.py`
- `paper_worker.py`
- `db_store.py`
- `dashboard.html`
- `signal_engine.py`
- `signal_fusion.py`

Add:

- `btc_trend_monitor.py`
- `test_btc_trend_monitor.py`
- `test_entry_quality.py`
- `test_performance_audit.py`
- `test_orderbook_quote_pricing.py`
- `test_market_data_diagnostics.py`
- `test_current_signal.py`
- `test_current_window_wait.py`
- `test_signal_engine.py`
- `test_wait_alerts.py`
- `README_BTC_BOT_CONSOLIDATED_UPGRADE.md`

`kalshi_api.py` and `test_kalshi_api.py` are included from the earlier public-API retry patch. Because that retry patch was already deployed according to our previous check, compare the included `kalshi_api.py` with your current copy; keep the deployed version if it contains the same retry improvements. Do not replace it with an older version.

Keep your existing project-specific `candle_feed.py`, `trend_stop.py`, `market_discovery.py`, Dockerfile, and existing dependency files. They are intentionally not recreated because they are project-specific and already exist in your repository. Keep all existing requirements, including `pandas`, FastAPI, Pydantic, and PostgreSQL driver dependencies.

## Render environment

Keep the same shared `DATABASE_URL` on the Web Service and Background Worker. The schema update creates a `btc_trend_monitor` table automatically.

Add/update the following values on **both** services where specified:

- `TRADING_MODE=paper` — both services
- `LIVE_TRADING_ENABLED=false` — both services
- `PAPER_ENTRY_TARGET_MAX_CENTS=80` — both services
- Keep the existing `PAPER_MAX_ENTRY_CENTS` hard cap unchanged (for example, 92)
- Keep `MAX_TRADE_DOLLARS=1.00` and `MAX_DAILY_LOSS_DOLLARS=10.00` unchanged

Add these optional controls to the **Background Worker**:

- `BTC_TREND_MONITOR_INTERVAL_SECONDS=30`
- `BTC_TREND_MONITOR_MAX_CACHE_AGE_SECONDS=45`

Do not set the Web Service to run a second worker. Keep the existing separate-worker architecture and existing signal-source/shared-database configuration.

## Deployment sequence

1. Make a backup/branch before replacing files.
2. Replace the listed files and add the new files at the repository root.
3. Compare `kalshi_api.py` against the already-deployed retry patch before overwriting it.
4. Commit with: `Add BTC trend monitor and entry quality controls`.
5. Wait for **both** the Web Service and Background Worker to finish deploying the same commit.
6. Set the environment values above without changing the risk limits or trading mode.
7. Check `/api/dashboard` for `btc_trend_monitor`, `/diagnostics/market-data` for current candle/orderbook health, and worker heartbeat.
8. Review paper trades before making any further strategy changes.

## Local checks

Run syntax checks:

```bash
python -m py_compile main.py paper_worker.py db_store.py signal_engine.py signal_fusion.py btc_trend_monitor.py
```

Run the trend monitor and pure unit tests in separate commands so test modules that stub missing project-specific feed modules do not share process state:

```bash
python -m unittest -v test_btc_trend_monitor.py
python -m unittest -v test_signal_engine.py
python -m unittest -v test_orderbook_quote_pricing.py
python -m unittest -v test_current_signal.py
python -m unittest -v test_current_window_wait.py
```

Run the integrated entry/performance/market-data tests in the actual repository environment where `candle_feed.py`, `trend_stop.py`, `market_discovery.py`, and the project's dependencies are present. The local packaging environment did not include those project-specific modules, so it cannot honestly verify full worker integration here.

## Safe operating rules

Do not enable real trading. Do not lower the entry target or increase trade size to force activity. The score is not a win probability. The monitor does not place trades by itself, and a fresh alert should still be rejected or skipped when any required input is stale or invalid.
