# Automated multi-market research-to-paper pipeline

## What the code does

1. The catalog worker paginates through open Kalshi markets up to its configured safety cap, counts every unique market returned, and keeps a bounded top-activity pool (volume/open interest) for deeper work. The dashboard polls a small inventory snapshot; the analysis worker receives the internal ranked pool. This avoids turning every dashboard refresh into a huge response.
2. The research pipeline rotates over a configurable batch of ranked markets, requests current market details and order-book depth, and calls the configured analysis/evidence service.
3. A market is eligible for a simulated paper entry only if the external service returns a fresh probability model with at least the minimum number of out-of-sample validation samples, acceptable Brier score, current source-attributed evidence required for the category, and exact market-ticker match.
4. The decision engine checks volume, both-side top-of-book spread and depth, maximum price, expected net edge after estimated fees and slippage, category allowlist, safe paper-mode environment, per-run entry cap, open-trade cap, open-cost cap, daily loss cap, and duplicate market/model/version.
5. Any eligible entry goes into `market_research_paper_trades`, a separate research-only hold-to-settlement paper journal. It does not share the BTC strategy position lifecycle, does not submit live orders, and does not count toward the BTC 15-minute win-rate card. Resolved market outcomes are reconciled from the public market endpoint and the journal subtracts the configured estimated fee and slippage.

## What it does not do

- It does not infer probabilities from a price, a headline, or a heuristic score alone.
- It does not fabricate news/injury data. Those facts must be supplied and time-stamped by the configured provider.
- It does not yet fetch or submit authenticated live orders. The real-trading mode stays server-locked.
- A full catalog inventory is not the same as deep-researching every market every cycle. By default the top 500 activity-ranked candidate pool is processed 10 markets per batch and rotated; API/source quotas make that safer and more sustainable. Configure pool and batch sizes only after testing provider limits.

## Provider configuration

Set `MARKET_ANALYSIS_PROVIDER_URL` to an **HTTPS** endpoint owned/selected by the operator. If it needs authentication, set `MARKET_ANALYSIS_PROVIDER_API_KEY`; the server sends it as a bearer token and never returns it to the browser. The request JSON has:

```json
{
  "contract": "kalshi-market-model-v1",
  "market": {"ticker": "...", "title": "...", "status": "active"},
  "orderbook_summary": {"yes_ask_cents": 51, "no_ask_cents": 51},
  "required_evidence_types": ["injury_report", "lineup", "recent_news", "recent_stats"]
}
```

The provider must return a JSON object in this shape (the values are illustrative only):

```json
{
  "market_ticker": "EXACT-SAME-TICKER",
  "model_probability_yes": 0.67,
  "model_id": "category-model-name",
  "model_version": "version-id",
  "as_of_utc": "2026-10-23T23:30:00Z",
  "validation": {"out_of_sample": true, "sample_count": 250, "brier_score": 0.17},
  "evidence": [
    {
      "type": "injury_report",
      "source": "Named source",
      "url": "https://source.example/verified-report",
      "published_at": "2026-10-23T23:25:00Z",
      "retrieved_at": "2026-10-23T23:29:00Z",
      "relevance_score": 0.95,
      "reliability_score": 0.95
    }
  ],
  "rationale": "A concise explanation, with uncertainty and contradictory facts included."
}
```

The API must return evidence matching the requested category and include all required types:

- `sports`: `injury_report`, `lineup`, `recent_news`, `recent_stats`
- `politics`: `official`, `polling`, `recent_news`
- `economics`: `official_release`, `consensus`, `recent_news`
- `weather`: `forecast`, `observation`
- `crypto_event`: `exchange_data`, `official`, `recent_news`
- `financial_markets`: `price_data`, `official`, `recent_news`
- `entertainment`: `official`, `recent_news`
- BTC 15m if explicitly added: `exchange_data`, `recent_news` (this research journal is not the existing BTC worker's strategy)

The code checks HTTPS provenance URLs, timestamps, source relevance/reliability scores, probability range, exact market-ticker binding, model timestamp, validation sample count, out-of-sample flag and Brier score. Safety minimums cannot be relaxed below 50 in 24-hour volume, 5 contracts of depth, 5¢ minimum net edge, 5¢ maximum spread, 90¢ maximum entry, 2¢ estimated fee, 1¢ estimated slippage, 100 out-of-sample samples and 0.22 maximum Brier score; configuration can tighten them further. These fields are still claims from the provider; this system does not independently certify the provider's model or fetch the source webpage to verify those claims. Provider procurement and a source-domain audit remain necessary before relying on automated entries.

## Environment variables

Start with:

- `MARKET_SCANNER_AUTO_START=true` — automatically inventory the open catalog.
- `MARKET_SCAN_MAX_PAGES=500` and `MARKET_SCAN_MAX_MARKETS=100000` — hard inventory safety caps.
- `MARKET_RESEARCH_POOL_SIZE=500` — top-activity pool for deep analysis.
- `MARKET_ANALYSIS_PROVIDER_URL=https://...` — required external analysis provider; no default provider exists.
- `MARKET_ANALYSIS_PROVIDER_API_KEY=...` — only if the provider needs it.
- `MARKET_ANALYSIS_AUTO_START=true` — periodically run research batches after the catalog is ready.
- `MARKET_ANALYSIS_INTERVAL_SECONDS=600` and `MARKET_ANALYSIS_MAX_PER_CYCLE=10`.
- `MARKET_AUTOPAPER_ENABLED=true` — only after provider contract and tests have been verified; defaults false.
- `MARKET_PAPER_ENABLED_CATEGORIES=sports,politics,economics,weather,crypto_event,financial_markets,entertainment` — each paper entry still needs all evidence/model gates.
- `MARKET_MODEL_MIN_VALIDATION_SAMPLES=100`, `MARKET_MODEL_MAX_BRIER_SCORE=0.22`.
- `MARKET_MIN_VOLUME_24H=50`, `MARKET_MIN_DEPTH_CONTRACTS=5`, `MARKET_MAX_SPREAD_CENTS=5`, `MARKET_MIN_NET_EDGE_CENTS=5`.
- `MARKET_ESTIMATED_FEE_CENTS=2`, `MARKET_ESTIMATED_SLIPPAGE_CENTS=1`, `MARKET_MAX_ENTRY_CENTS=90`.
- `MARKET_RESEARCH_MAX_OPEN_TRADES=20`, `MARKET_RESEARCH_MAX_OPEN_COST_CENTS=2000`, `MARKET_RESEARCH_MAX_DAILY_LOSS_CENTS=500`, `MARKET_RESEARCH_MAX_NEW_TRADES_PER_RUN=5`.
- Keep `TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false` in all services.

Set the scheduler variables on the **Web Service**, where the FastAPI market research pipeline runs. Preserve the same existing `DATABASE_URL` on the Web Service and Background Worker for shared journaling. Do not set the auto-paper switch until the provider responds with the agreed schema in a staging deployment. These fee/slippage settings are configurable estimates, not a replacement for contract-specific exchange fee calculation. Kalshi notes that fee schedules differ for some markets, and resting-order maker fees may apply to some products; consult the current official fee schedule and use a higher estimate for special contracts. The `2¢` default assumes a one-contract base taker fee under the general schedule; it is not guaranteed for every series. For the strategy metrics, also account for the separate fee on any exit or close if the simulated strategy does not hold to settlement.

## Evaluating win rate

The user's reported 95 trades and 56% observed win rate imply roughly 53 wins and 42 non-wins if all 95 are settled; confirm with the actual journal because open trades and break-even results can change that count. The 95% Wilson interval for 53/95 is approximately 45.8%–65.4%, which is wide. Do not tune strategy based on this sample alone. Evaluate fee-inclusive average P&L per trade, profit factor, drawdown, entry-price cohort, trend alignment, signal freshness, and out-of-sample results as well as hit rate. The research-pipeline gates deliberately start strict and are paper-only so it can collect independent hold-to-settlement samples without altering the running BTC strategy.


## Validation runtime status (important)

Latest local validation: 215 tests passed; 25,000 market decision properties passed; 100,000 Phase 4–6 metadata fuzz cases, 20,000 mode gate cases and 10,000 token uniqueness checks passed; offline webhook-to-paper smoke test passed; dashboard JavaScript syntax check passed. The requested two-hour soak has NOT completed in this environment. `run_validation_soak.py` ships with the project and defaults to 7,200 seconds of actual test-command runtime without counting sleep. Check its JSON report and log; require `status=passed` and `completed_test_command_seconds >= 7200`.


## Live calibration from settled outcomes

The app stores only model forecasts made before settlement, then checks aged markets and writes true outcomes to `market_research_prediction_outcomes`. Model validation is computed per model/version/category in the provider service from that table. When the provider or database is unavailable, validation fails closed. Historical exports are available through the admin-protected `/api/diagnostics/replay-export`; `audit_historical_export.py` computes actual recorded trade results and cohort summaries. This audits available records and is not a complete counterfactual replay if the historical quote/news snapshots were not retained.
