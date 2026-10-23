# Order-book quote consistency fix (paper-only)

## What the diagnostic showed

The market diagnostic reported `yes_bid=49`, `yes_ask=47`, `no_bid=50`, and `no_ask=54`. The asks were read from the market-summary object while the bids were read from the current order book, so the code compared values that did not necessarily represent one consistent snapshot.

Kalshi order-book responses provide bid levels for YES and NO outcomes. The implied asks should be calculated from the opposite-side best bid:

- YES ask = 100 cents − best NO bid
- NO ask = 100 cents − best YES bid

With bids 49¢ YES and 50¢ NO, the implied asks are 50¢ YES and 51¢ NO; both spreads are 1¢. Market-summary asks are now displayed only as diagnostic comparison fields and are not used to price a simulated entry.

Reference: Kalshi's order-book documentation explains that the order book returns bids only and that asks are implied from the opposite side: https://docs.kalshi.com/getting_started/orderbook_responses

## Files to replace/add

Replace the repository root files:

- `main.py`
- `paper_worker.py`

Add or replace these test files:

- `test_market_data_diagnostics.py`
- `test_orderbook_quote_pricing.py`

Do **not** replace `kalshi_api.py` from the previously deployed rate-resilience patch. Keep the existing `candle_feed.py`, `market_discovery.py`, `trend_stop.py`, `signal_engine.py`, `signal_fusion.py`, `db_store.py`, and other project-specific modules.

## Behavior changes

- The market-data diagnostic calculates bid/ask from a single current order-book snapshot.
- The paper worker uses orderbook-implied asks for candidate entry price and spread checks.
- Take-profit reversal analysis uses the same orderbook-implied asks.
- Diagnostic candle age is measured from the approximate candle close, while exposing open time separately; this avoids labeling the candle open time as its close time.
- Missing bids still fail closed; no prices are invented when one side is missing.

## Deploy and verify

1. Commit the four files above.
2. Let both the Web Service and Background Worker redeploy the same commit.
3. Open `/diagnostics/market-data` once (it caches results for 60 seconds).
4. Confirm quotes are derived from the order book, no crossed quote is reported, and `ready_for_evaluation` accurately reflects both feed freshness and usable quotes.

This change does not tune signal scoring, lower filters, or place trades. Keep `TRADING_MODE=paper`, `LIVE_TRADING_ENABLED=false`, the 92¢ max-entry ceiling, $1 max trade size, and $10 daily-loss limit.
