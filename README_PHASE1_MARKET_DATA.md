# Phase 1 — Market-data observability and Kalshi request resilience

This is a narrow paper-only patch. It does not alter signal scoring, entry approval rules, contract sizing, or the 92-cent maximum entry configuration.

## What was confirmed in the current code

- `candle_feed.py` fetches fully closed BTCUSDT candles from Binance for 1m, 5m, and 15m; it rejects stale candle data.
- `paper_worker.py` uses those candles when evaluating an external/hybrid alert and reads public Kalshi market details/order-book quotes before a candidate entry.
- During idle `WAIT` cycles the worker intentionally does not continuously poll candle data.
- The previous `/status` string saying market data was not connected was inaccurate/outdated.
- Earlier Render logs contained Kalshi HTTP 429 errors, while the previous `kalshi_api.py` did not retry or back off.

## Files to replace/add

Replace at repository root:

- `main.py`
- `kalshi_api.py`

Add:

- `test_market_data_diagnostics.py`
- `test_kalshi_api.py`
- this README

Keep your existing `candle_feed.py`, `market_discovery.py`, `trend_stop.py`, Dockerfile, and other project-specific modules unchanged. The patched client preserves the existing functions: `get_markets`, `get_market`, `get_orderbook`, and `find_open_btc_markets`.

## What changes

- `/status` now truthfully reports that Binance candle data and Kalshi market/order-book data are retrieved on-demand during evaluation, not continuously while the bot is waiting for an external alert.
- New read-only `GET /diagnostics/market-data` checks the latest closed 1m/5m/15m Binance candles plus one current, validated KXBTC15M market's detail/order-book quotes. It returns timestamps, data ages, quote completeness, and `ready_for_evaluation`.
- The diagnostics route caches its result for 60 seconds so repeated visits do not create unnecessary external API traffic.
- The Kalshi public GET client adds per-process request spacing, bounded retry with backoff for transient network/429/5xx errors, and respects `Retry-After`. It does not add order endpoints or credentials.

## Deploy

1. Replace `main.py` and `kalshi_api.py` in GitHub with these versions.
2. Add both test files and this README.
3. Commit as `Improve market-data diagnostics and Kalshi rate resilience`.
4. Let Render deploy the same commit to the Web Service and Background Worker, because both import `kalshi_api.py`.
5. Keep `DATABASE_URL` the same in both services, `PAPER_SIGNAL_SOURCE=hybrid`, `TRADING_MODE=paper`, `LIVE_TRADING_ENABLED=false`, and the Web Service's `PAPER_WORKER_ENABLED=false` when using a separate Background Worker.
6. After both deployments are Live, open `/diagnostics/market-data` once. Do not hammer-refresh it; it is cached for 60 seconds.

## Tests

Run locally or through your existing CI:

```bash
python -m unittest -v test_market_data_diagnostics.py test_kalshi_api.py test_current_signal.py test_current_window_wait.py test_wait_alerts.py test_signal_engine.py test_paper_bot_fixes.py
python -m py_compile main.py kalshi_api.py paper_worker.py signal_engine.py signal_fusion.py db_store.py
```

`/diagnostics/market-data` is a read-only smoke test. It does not place trades. `ready_for_evaluation=true` means the source data passed basic availability/freshness/quote checks; it is not evidence that a signal has positive expected value or that the strategy is profitable.
