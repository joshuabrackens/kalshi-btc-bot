# Kalshi BTC 15-Minute Bot — Downloadable Starter

## Current capabilities
- FastAPI service, health/status endpoints
- Rule-based multi-timeframe signal engine
- SQLite signal journal
- Paper-trade ledger with manual simulated entries, settlement, fees, and basic stats
- Duplicate-open-trade guard and configurable daily paper-trade cap (maximum 96/day)
- Public Kalshi market discovery, market detail, and order-book endpoints
- Dockerfile for cloud deployment
- No private API key required for public market-data endpoints

## Important limitations
This is a deployable starter, not a finished unattended trading system. It does not yet:
- Fetch/aggregate live BTC 1m, 5m, and 15m OHLCV candles from a selected source
- Verify which candidate is the exact intended 15-minute contract and its settlement source
- Convert Kalshi market fields into a fully verified contract context automatically
- Send Telegram notifications
- Automatically simulate fills from a live signal stream; paper entries are recorded manually
- Guarantee realistic fills (paper entries can still differ from real execution)
- Place orders (real trading is not implemented and is disabled)

The score is a heuristic score, not a calibrated probability of winning.

## Run locally
Python 3.11+ recommended.

```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cd app
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/docs`.

Endpoints:
- `GET /` — service summary
- `GET /health` — health check
- `GET /status` — runtime status
- `GET /markets/btc-candidates` — candidate open BTC/Bitcoin markets
- `GET /markets/{ticker}` — public market details
- `GET /markets/{ticker}/orderbook` — public order book
- `POST /evaluate` — evaluate supplied candles
- `GET /journal/recent` — latest signal records
- `POST /paper/open` — record a simulated UP/DOWN entry (never places a live order)
- `POST /paper/{trade_id}/settle` — record the official final outcome and calculate net P/L
- `GET /paper/trades` — paper-trade ledger
- `GET /paper/stats` — settled-trade win rate and net P/L

## Paper-trading workflow
1. Run `POST /evaluate` with verified candle and contract snapshots. A signal score is a heuristic, not a win probability.
2. Only if the setup passes your own rules, manually record it with `POST /paper/open`, including the actual observed entry price and estimated fee per contract.
3. After the contract resolves, call `POST /paper/{trade_id}/settle` with the official `UP` or `DOWN` outcome.
4. Review `GET /paper/stats` and `GET /paper/trades`. Include fees and avoid assuming every signal can fill at the displayed ask.
5. Aim for up to 30 quality trades/day only if setups exist; 96 is a hard paper-ledger cap, not a quota. Never force trades to hit a count.

Example paper entry body:
```json
{
  "market_ticker": "PASTE_VERIFIED_TICKER_HERE",
  "direction": "UP",
  "contracts": 1,
  "entry_cents": 48.0,
  "fee_cents_per_contract": 1.0,
  "notes": "Example only; replace with a verified market and actual observed price"
}
```
The API rejects more than 10 contracts per paper entry and caps daily entries at 96 by default. Set `PAPER_MAX_TRADES_PER_DAY` to a lower value if desired. This is a record-keeping safeguard, not a guarantee of risk control or profitability.

## Cloud deployment
1. Create a private Git repository and upload these files.
2. Create an always-on Docker service on a cloud host.
3. Set the build source to this repository and use the included `Dockerfile`.
4. Set health check to `/health`.
5. Attach persistent storage for the journal and set `BOT_DB_PATH` to a path on it.
6. Keep the service private or add authentication before exposing it publicly.
7. Public market endpoints need no private API key. Do not provide credentials until an authenticated feature is implemented.
8. No real-order code is included. The paper endpoints only write simulated records; they do not execute trades.
9. Add authentication before exposing the API publicly. Use persistent storage for the SQLite journal.

## Verify candidate markets
After running, open `/docs` and call `GET /markets/btc-candidates`. Inspect the title, ticker, close time, expiration, strike/settlement information, and order-book fields against the current Kalshi market page. This discovery endpoint is best-effort and must not be treated as proof that a returned market is the exact target.

## Next milestones
1. Verify official API endpoints and identify the exact BTC 15-minute market series and settlement source.
2. Implement a selected BTC candle provider and timestamp alignment.
3. Normalize Kalshi order book and contract asks/bids correctly for the UP/DOWN market.
4. Add Telegram notifications.
5. Add paper-trading fills, settlement tracking, fees/slippage, and performance reports.
6. Test on out-of-sample data and inspect drawdowns, not just win rate.
7. Only after validation, design authenticated order placement with hard limits, duplicate protection, reconciliation, and kill switch.
