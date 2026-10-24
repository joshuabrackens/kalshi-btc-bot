# BTC Bot deployment guide — all phases

## Download and deployment

- Preserve the currently working production commit/tag first.
- Apply this project snapshot on a separate upgrade branch and review changed files.
- Run GitHub Actions on that exact commit.
- Set the environment values below and deploy both Render services from the same commit.
- Verify Web Service health, worker heartbeat, real Kalshi public-market scan status, fresh webhook → worker → paper decision, and database persistence before promoting anything.
- Do not include a local `bot_journal.sqlite3` in a repository/package; live trade records and credentials must use the configured shared database.

## Render Web Service — add/check

| Variable | Value |
|---|---|
| `KALSHI_BASE_URL` | `https://external-api.kalshi.com/trade-api/v2` |
| `KALSHI_VAULT_ADMIN_SECRET` | A random secret with at least 32 characters; keep it private |
| `PAPER_ADMIN_API_SECRET` | Leave blank unless manual paper journal/evaluation writes are intentionally needed |
| `KALSHI_CREDENTIALS_ENCRYPTION_KEY` | A Fernet key generated once; keep it stable |
| `MARKET_SCANNER_AUTO_START` | `true` |
| `MARKET_SCAN_MAX_PAGES` | `500` |
| `MARKET_SCAN_MAX_MARKETS` | `100000` |
| `MARKET_SCAN_REFRESH_SECONDS` | `600` |
| `MARKET_SCAN_RETRY_SECONDS` | `60` |
| `MARKET_RESEARCH_POOL_SIZE` | `500` |
| `MARKET_ANALYSIS_PROVIDER_URL` | Your vetted HTTPS probability/evidence provider (required for automatic deeper analysis) |
| `MARKET_ANALYSIS_PROVIDER_API_KEY` | Provider API key, only if required |
| `MARKET_ANALYSIS_AUTO_START` | `true` after provider validation |
| `MARKET_ANALYSIS_INTERVAL_SECONDS` | `600` |
| `MARKET_ANALYSIS_MAX_PER_CYCLE` | `10` |
| `MARKET_AUTOPAPER_ENABLED` | `false` until provider data and gates have passed staging validation |
| `MARKET_PAPER_ENABLED_CATEGORIES` | `sports,politics,economics,weather,crypto_event,financial_markets,entertainment` (only researched categories) |
| `MARKET_MIN_NET_EDGE_CENTS` | `5` |
| `MARKET_MIN_DEPTH_CONTRACTS` | `5` |
| `MARKET_MAX_SPREAD_CENTS` | `5` |
| `MARKET_MODEL_MIN_VALIDATION_SAMPLES` | `100` |
| `MARKET_MODEL_MAX_BRIER_SCORE` | `0.22` |
| `TRADING_MODE` | `paper` |
| `LIVE_TRADING_ENABLED` | `false` |
| `DATABASE_URL` | Keep the existing Render PostgreSQL URL |

## Render Background Worker — check

| Variable | Value |
|---|---|
| `KALSHI_BASE_URL` | Same current URL above |
| `KALSHI_CREDENTIALS_ENCRYPTION_KEY` | Exactly the same key as the Web Service |
| `TRADING_MODE` | `paper` |
| `LIVE_TRADING_ENABLED` | `false` |
| `PAPER_SIGNAL_SOURCE` | `hybrid` (only if this matches your validated current intended strategy) |
| `DATABASE_URL` | Exactly the same PostgreSQL database as the Web Service |

Keep all other existing risk limits unchanged. Do not paste secrets into GitHub. In Render, environment variables/secrets belong in the service's Environment settings, not in source files.

Generate the vault encryption key locally once, using Python after installing `cryptography`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy that one generated value into `KALSHI_CREDENTIALS_ENCRYPTION_KEY` for BOTH services, and save it securely in your own password manager. Do not regenerate it during redeploys.

## One-time Kalshi key storage

Kalshi authenticated access uses an API Key ID and its associated private key (PEM), not just a lone secret string. After `KALSHI_VAULT_ADMIN_SECRET` and `KALSHI_CREDENTIALS_ENCRYPTION_KEY` are configured and deployed, unlock the dashboard's credential vault and save the pair to the correct environment (`Demo` first; production can be stored separately later). The app stores the pair encrypted in the database and only returns a masked key hint. Verify only with the read-only balance check. Never send the PEM in screenshots, chat, logs, GitHub issues, or browser localStorage.

## Trading-mode safety

- The dashboard has a Paper/Real selector, but Real mode is intentionally **server-side locked** in this release. The package contains a credential vault, not a production order execution adapter.
- Do **not** change `TRADING_MODE=real` or `LIVE_TRADING_ENABLED=true`. The worker must remain paper-only until order submit/cancel, idempotency, fill/position reconciliation, fee/slippage handling, emergency stop, and Kalshi Demo end-to-end tests are implemented and reviewed.
- Because the real-order executor is not implemented, this version cannot yet make live execution available just by toggling a button. A true pause/resume control must also reconcile and cancel any resting orders before claiming to be paused.

## Market-scanner and research-pipeline boundaries

The scanner inventories open Kalshi markets via cursor pagination, at most 500 pages / 100,000 distinct market records per pass by default, with a 10-minute refresh. It shows up to 50 market examples and counts the scanned catalog. Reaching a cap or an upstream failure is reported explicitly as `partial`/`failed`. Deeper research is limited to a top-activity pool (500 by default) and rotates through 10 candidates per batch; therefore it does not deep-analyze every market in every cycle.

The new `market_decision_pipeline.py` requests a calibrated probability and current, source-attributed evidence from `MARKET_ANALYSIS_PROVIDER_URL`, then checks market status, order book, spread, depth, expected net edge after configurable fee/slippage estimates, validation sample count/Brier score, category evidence requirements, and separate research-paper exposure/loss limits. It may record isolated hold-to-settlement paper simulations only when explicitly enabled. It does not submit live orders. The provider-service source is bundled, but it must be deployed and configured as a separate HTTPS service; until it has been configured and verified, the pipeline will report `provider_not_configured` and generate no research entries. Provider-provided source URLs/reliability scores are claims that require a source-domain audit.

The 95 trade / 56% win-rate figure should be treated as provisional. The analytics code reports a Wilson confidence interval and fee-adjusted P&L metrics. Do not tune model thresholds on those 95 trades alone or optimize hit rate without regard to average win/loss. Validate changes on a chronological, unseen sample and compare net expectancy, profit factor and drawdown. Only the original BTC 15-minute strategy runs through `paper_worker.py`; separate research-paper categories never reuse its signal logic.


### Separate market-analysis provider service

Deploy `market_analysis_provider_service.py` as a separate Render Web Service using `uvicorn market_analysis_provider_service:app --host 0.0.0.0 --port $PORT`. Configure `OPENAI_API_KEY` from the operator's OpenAI Platform, `OPENAI_MODEL` to a Responses-API model accessible to the account that supports web search + structured JSON, the generated `MARKET_ANALYSIS_PROVIDER_API_KEY`, and the same `DATABASE_URL` as the main app. On the main web service/worker, configure `MARKET_ANALYSIS_PROVIDER_URL` to the provider's HTTPS `/analyze` endpoint and set the same shared token. Do not enable `MARKET_AUTOPAPER_ENABLED` until the desired category/model has real resolved prediction outcomes passing calibration and it has run safely in staging.

The provider returns research probabilities and actual cited-source evidence only. The main decision engine independently checks ticker, freshness, evidence requirements, spread/depth, fee/slippage and model calibration. Neither service places live orders through the research pipeline.

### Historical data review

After deploying this version, unlock dashboard admin controls and download `/api/diagnostics/replay-export`. Run `python audit_historical_export.py btc_bot_historical_replay_export.zip`. Review the output before tuning the BTC strategy. The actual Render production journal cannot be simulated from a local source snapshot; use an export from the live database.
