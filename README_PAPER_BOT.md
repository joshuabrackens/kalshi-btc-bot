> **Historical note:** This file describes an earlier, individual patch and is retained for context. For the current reconciled repository, environment settings, test results, and deployment checklist, use `README.md`, `BTC_BOT_MASTER_CHANGELOG.md`, and `PACKAGE_MANIFEST.md`. Do not apply an old file list in isolation.

# Kalshi BTC 15-minute bot — paper-only worker

## What this version does
- Keeps all trading simulated. It has no authenticated Kalshi order-placement code.
- Fetches closed BTC/USDT candles from Binance public market data.
- Uses the existing signal engine and public Kalshi market/order-book endpoints.
- Opens at most one paper position at a time and applies the configured per-trade cost cap.
- Monitors the relevant public bid for a simulated stop-loss exit.
- Settles only when the Kalshi market response explicitly reports a final `yes` or `no` result.
- Fails closed when strike, timing, title, or bid/ask data cannot be verified.

The signal score is a heuristic, not a calibrated probability or a promise of profit. Paper stop-loss fills are simulated at the observed bid and may not match a real executable fill.

## Files in this bundle
Upload these files into the repository root, replacing the same-named files where they already exist:
- `main.py`
- `signal_engine.py`
- `market_discovery.py`
- `kalshi_api.py`
- `candle_feed.py`
- `paper_worker.py`
- `requirements.txt`

`test_paper_worker.py` is included for local safety checks; it does not need to be deployed.

## Render environment variables
Keep these values:
- `TRADING_MODE=paper`
- `MAX_TRADE_DOLLARS=1.00`
- `MAX_DAILY_LOSS_DOLLARS=10.00`
- `LIVE_TRADING_ENABLED=false`

Initially set:
- `PAPER_WORKER_ENABLED=false`

After the updated code deploys and `/status` confirms the app is healthy, check `/markets/btc-candidates` and `/paper/stats`. The worker is deliberately strict: if market data does not expose a verified strike, correct 15-minute expiry, and both sides' bid/ask, it will log a wait instead of opening a trade.

Only after checking Render logs and confirming the worker behaves as expected in paper mode should you change `PAPER_WORKER_ENABLED=true`. That starts a background paper-only worker inside the web service, sharing the same configured SQLite database. Keep `LIVE_TRADING_ENABLED=false` permanently for this version.

## Run tests locally
```bash
python -m pip install -r requirements.txt
python -m unittest -v test_paper_worker.py
python -m py_compile main.py paper_worker.py signal_engine.py market_discovery.py kalshi_api.py candle_feed.py
```

## Important limitations
- This is not a real-money trading bot and must not be treated as one.
- Market discovery and market field formats can change; missing/unrecognized fields cause the worker to skip trading.
- SQLite persistence on Render depends on the service's persistent disk and `BOT_DB_PATH` configuration.
- The worker runs inside the web process. Do not run multiple web workers/instances against the same SQLite database, because that could create duplicate paper entries.
