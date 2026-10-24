# BTC Bot — Scanner, Autopilot, and Provider Environment Guide

Use a separate Git branch first. These settings enable **research-paper automation only**, not real order placement.

## Web Service (main bot)

Keep existing `DATABASE_URL`, webhook secret, and established BTC risk limits unchanged. Verify/add:

| Variable | Value |
|---|---|
| `KALSHI_BASE_URL` | `https://external-api.kalshi.com/trade-api/v2` |
| `KALSHI_MIN_REQUEST_INTERVAL_SECONDS` | `0.5` |
| `KALSHI_GET_MAX_RETRIES` | `4` |
| `MARKET_SCANNER_AUTO_START` | `true` |
| `MARKET_SCAN_PAGE_SIZE` | `200` |
| `MARKET_SCAN_MAX_PAGES` | `1000` |
| `MARKET_SCAN_MAX_MARKETS` | `200000` |
| `MARKET_SCAN_PAGE_DELAY_SECONDS` | `0.05` |
| `MARKET_SCAN_PAGE_RETRIES` | `1` |
| `MARKET_SCAN_REFRESH_SECONDS` | `600` |
| `MARKET_SCAN_RETRY_SECONDS` | `60` |
| `KALSHI_VAULT_ADMIN_SECRET` | Generated app-side secret; keep private |
| `KALSHI_CREDENTIALS_ENCRYPTION_KEY` | Generated Fernet key; keep stable and back it up securely |
| `MARKET_ANALYSIS_PROVIDER_URL` | `https://<provider-service>.onrender.com/analyze` |
| `MARKET_ANALYSIS_PROVIDER_API_KEY` | Shared app-to-app bearer token (same as provider service) |
| `MARKET_ANALYSIS_PROVIDER_TIMEOUT_SECONDS` | `8` |
| `MARKET_ANALYSIS_AUTO_START` | `false` until provider setup is validated; dashboard toggle starts scheduler when enabled |
| `MARKET_AUTOPAPER_ENABLED` | `false` until provider/calibration is reviewed; set `true` only when ready for eligible research-paper entries |
| `MARKET_PAPER_ENABLED_CATEGORIES` | `sports,politics,economics,weather,crypto_event,financial_markets,entertainment,companies,world,science_technology` |
| `TRADING_MODE` | `paper` |
| `LIVE_TRADING_ENABLED` | `false` |
| `LIVE_TRADING_APPROVED` | `false` |
| `MARKET_LIVE_EXECUTION_ENABLED` | `false` |

The scanner covers both `/events` and `/events/multivariate`. It is bounded (up to 1,000 pages and 200,000 distinct markets by default) and reports `partial` if a cap or failure interrupts inventory. The deep research pipeline rotates through an activity-ranked pool (500 by default), analyzes up to 10 markets per 300-second cycle, and never analyzes every market simultaneously.

## Background Worker

Use the exact same `DATABASE_URL`, `KALSHI_BASE_URL`, and `KALSHI_CREDENTIALS_ENCRYPTION_KEY` as the Web Service. Keep:

- `TRADING_MODE=paper`
- `LIVE_TRADING_ENABLED=false`
- `LIVE_TRADING_APPROVED=false`
- `MARKET_LIVE_EXECUTION_ENABLED=false`
- `PAPER_SIGNAL_SOURCE=hybrid` only if that matches the validated existing configuration.

If paper work runs in a separate Background Worker, leave `PAPER_WORKER_ENABLED=false` on the Web Service.

## Separate analysis provider service

Deploy the same repository as a separate Render Web Service with start command:

```bash
uvicorn market_analysis_provider_service:app --host 0.0.0.0 --port $PORT
```

Set:

| Variable | Value |
|---|---|
| `OPENAI_API_KEY` | Create in your own OpenAI Platform account; never commit it |
| `OPENAI_MODEL` | A Responses API model enabled for your account with web search and structured-output support |
| `MARKET_ANALYSIS_PROVIDER_API_KEY` | Same shared app-to-app token as the main Web Service/Worker |
| `DATABASE_URL` | Same internal PostgreSQL URL so calibration reads resolved outcomes |
| `OPENAI_REQUEST_TIMEOUT_SECONDS` | `35` |

On the provider service, `MARKET_ANALYSIS_PROVIDER_API_KEY` is used to authenticate the bot-to-provider request. The main bot now treats the provider as configured only when both its HTTPS URL and this non-empty bearer token exist.

## How automatic paper research works

1. In the dashboard, unlock secure controls and turn the master **Market Autopilot** toggle ON.
2. The toggle starts/resumes the paginated catalog inventory and the research-analysis scheduler.
3. The pipeline analyzes the activity-ranked candidate pool and evaluates exact ticker, category-specific current evidence, model freshness, actual orderbook depth/spread, fees/slippage, net edge, category allowlist, current market status, and exposure/daily-loss limits.
4. It writes an isolated paper research trade only if all required checks pass.
5. If the provider is missing/unavailable or calibration isn't passed, no research entry is created. The model must have at least 100 subsequent resolved forecasts for the exact model/version/category, both YES and NO outcomes represented, and Brier score <= 0.22. This warm-up is intentional.
6. Toggle OFF to pause new automatic scans/decisions. A scan already in progress can finish its bounded cursor pass; the catalog scanner itself never places orders.

## Diagnose scanner continuation failure

In the Render **Web Service Shell**, after deploying the code patch, run:

```bash
python diagnose_kalshi_catalog.py --pages 5
```

It checks up to five pages from each public event feed and prints HTTP status, response shape, market counts, and cursor length only. It never prints cursor values or response bodies. Share the result if the scanner still fails.

## Live orders

The current research-autopilot switch only enables scanning and paper research entries. Actual live-order submission remains locked until the signed order adapter is integrated at the app level and a complete Kalshi Demo lifecycle (order submission, fills, partial fills, cancel, retry/idempotency, reconcile after timeout/restart, fees/position verification, emergency pause) passes. Do not set live flags true to bypass that gate.
