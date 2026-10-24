# BTC Bot all-phases setup — quickstart

This guide covers the current source bundle, the automated market research provider, the one-time Kalshi credential form, paper-only automatic scanning, historical audit, and the existing fail-closed live order adapter.

## Before anything else

1. Preserve the current working Git commit and create an upgrade branch. The ZIP is a project snapshot, not a reconciliation against your live GitHub repository.
2. Put the source into the upgrade branch and run GitHub Actions.
3. Deploy the main Web Service and Background Worker from the same commit to a staging/paper environment.
4. Never commit `.env`, the generated app-secret file, OpenAI keys, or Kalshi private key files.

## Generated app-side secrets

Use the separate file `BTC_Bot_Generated_App_Secrets.txt` delivered with this package. It contains random values generated for:

- `KALSHI_VAULT_ADMIN_SECRET` — unlocks protected dashboard admin controls.
- `KALSHI_CREDENTIALS_ENCRYPTION_KEY` — Fernet at-rest encryption for stored Kalshi credentials. Keep it stable forever unless you intentionally decrypt/re-encrypt all saved credentials.
- `MARKET_ANALYSIS_PROVIDER_API_KEY` — a private bearer token shared between the main bot and the provider service.

Do not copy that secret file into the repository ZIP. Keep the original in a secure password manager after you put the values into Render.

The generated file does **not** contain real vendor credentials. You must create an `OPENAI_API_KEY` in your OpenAI Platform project, and create the Kalshi API Key ID/private-key file in your own Kalshi account. Never send the Kalshi private key or OpenAI API key in chat.

## Render service A: main BTC bot Web Service

Add/update these variables:

| Variable | Value | Notes |
|---|---|---|
| `DATABASE_URL` | existing Render internal PostgreSQL URL | Same DB on Web Service and Background Worker |
| `TRADING_MODE` | `paper` | Keep this while validating |
| `LIVE_TRADING_ENABLED` | `false` | Do not switch on for this package |
| `PAPER_SIGNAL_SOURCE` | `hybrid` | Only if this matches the validated current worker setup |
| `PAPER_WORKER_ENABLED` | `false` | If the separate Background Worker is responsible for paper execution |
| `KALSHI_BASE_URL` | `https://external-api.kalshi.com/trade-api/v2` | Current production public API base for public market data; the saved credential environment selects demo vs production for private calls |
| `KALSHI_VAULT_ADMIN_SECRET` | generated app secret | Same on any service that needs admin verification; never share it |
| `KALSHI_CREDENTIALS_ENCRYPTION_KEY` | generated Fernet key | Same on Web Service and Background Worker; stable value |
| `GAINZALGO_WEBHOOK_SECRET` | keep existing secret | Do not rotate as part of this update |
| `GAINZALGO_WEBHOOK_MAX_BODY_BYTES` | `16384` | Keep bounded |
| `MARKET_SCANNER_AUTO_START` | `true` | Automatic public market inventory scan |
| `MARKET_SCAN_MAX_PAGES` | `500` | Bounded paging cap |
| `MARKET_SCAN_MAX_MARKETS` | `100000` | Maximum catalog records per scan; interrupted/capped scans must show partial status |
| `MARKET_SCAN_PAGE_DELAY_SECONDS` | `0.05` | Small inter-page delay |
| `MARKET_SCAN_REFRESH_SECONDS` | `600` | Ten-minute catalog refresh |
| `MARKET_SCAN_RETRY_SECONDS` | `60` | Retry failed scan after delay |
| `MARKET_ANALYSIS_PROVIDER_URL` | `https://<provider-service>.onrender.com/analyze` | Set after deploying provider service |
| `MARKET_ANALYSIS_PROVIDER_API_KEY` | generated app-to-app token | Same exact value on main bot and provider service |
| `MARKET_ANALYSIS_PROVIDER_TIMEOUT_SECONDS` | `8` | Bounded request timeout |
| `MARKET_ANALYSIS_AUTO_START` | `true` | Starts background scheduler; analysis cannot proceed if provider is missing |
| `MARKET_ANALYSIS_INTERVAL_SECONDS` | `600` | Scan/analysis cadence |
| `MARKET_ANALYSIS_MAX_PER_CYCLE` | `10` | Maximum markets sent to deep analysis per cycle |
| `MARKET_RESEARCH_POOL_SIZE` | `500` | Rotating analysis candidate pool |
| `MARKET_OUTCOME_MIN_AGE_MINUTES` | `60` | Do not label a forecast immediately after creation |
| `MARKET_OUTCOME_RETRY_MINUTES` | `60` | Avoid repeatedly fetching unresolved markets |
| `MARKET_OUTCOME_COLLECT_MAX_PER_RUN` | `20` | Bound market-resolution lookups |
| `MARKET_AUTOPAPER_ENABLED` | `true` only when ready for research paper automation | Upper safety gate; dashboard master toggle still defaults OFF and must be enabled by an authenticated admin |
| `MARKET_PAPER_ENABLED_CATEGORIES` | `sports,politics,economics,weather,crypto_event,financial_markets,entertainment` | Each category still needs validated model/evidence, liquidity and edge checks |
| `MARKET_MIN_VOLUME_24H` | `50` | Minimum volume gate; cannot relax the engine's hard floor |
| `MARKET_MIN_DEPTH_CONTRACTS` | `5` | Minimum top-of-book depth floor |
| `MARKET_MAX_SPREAD_CENTS` | `5` | Maximum spread |
| `MARKET_MIN_NET_EDGE_CENTS` | `5` | Minimum estimated edge after costs |
| `MARKET_ESTIMATED_FEE_CENTS` | `2` | Conservative estimated fee; update from actual fee schedule as appropriate |
| `MARKET_ESTIMATED_SLIPPAGE_CENTS` | `1` | Conservative slippage assumption |
| `MARKET_MAX_ENTRY_CENTS` | `90` | Hard price ceiling for research paper entries |
| `MARKET_MODEL_MIN_VALIDATION_SAMPLES` | `100` | Minimum subsequently resolved outcomes per model/category |
| `MARKET_MODEL_MAX_BRIER_SCORE` | `0.22` | Model calibration threshold |
| `MARKET_RESEARCH_MAX_OPEN_TRADES` | `20` | Isolated research-paper open-trade cap |
| `MARKET_RESEARCH_MAX_OPEN_COST_CENTS` | `2000` | Isolated research-paper exposure cap |
| `MARKET_RESEARCH_MAX_DAILY_LOSS_CENTS` | `500` | Isolated research-paper loss cap |
| `MARKET_RESEARCH_MAX_NEW_TRADES_PER_RUN` | `5` | Isolated research-paper per-run entry cap |

Keep existing BTC risk limits (including `MAX_TRADE_DOLLARS`, `MAX_DAILY_LOSS_DOLLARS`, `PAPER_MINIMUM_SCORE`, `PAPER_MAX_ENTRY_CENTS`, `PAPER_ENTRY_TARGET_MAX_CENTS`, and stop/take-profit settings) unchanged unless intentionally reviewed.

## Render service B: separate market-analysis provider

Deploy the same repository as a separate Render Web Service.

**Start command:**

```bash
uvicorn market_analysis_provider_service:app --host 0.0.0.0 --port $PORT
```

Set these variables on the provider service:

| Variable | Value |
|---|---|
| `OPENAI_API_KEY` | Key generated from your own OpenAI Platform project; never commit it |
| `OPENAI_MODEL` | `gpt-5.5` if this model is enabled for your API account; otherwise select an account-enabled Responses API model that supports web search and structured outputs |
| `MARKET_ANALYSIS_PROVIDER_API_KEY` | Same generated app-to-app token as the main service |
| `DATABASE_URL` | Same Render internal PostgreSQL URL as the main Web Service/Worker |
| `OPENAI_REQUEST_TIMEOUT_SECONDS` | `35` |
| `MARKET_MODEL_MIN_VALIDATION_SAMPLES` | `100` |
| `MARKET_MODEL_MAX_BRIER_SCORE` | `0.22` |
| `MARKET_MODEL_VALIDATION_REPORT_PATH` | Optional path to an offline replay-generated registry; the shared DB is the primary source when DATABASE_URL is configured |

The provider uses the OpenAI Responses API web-search tool and structured output. It returns cited research, not execution authorization. The provider itself never places orders. Realized-outcome calibration comes from the shared prediction-outcome journal, not from the model grading itself.

## Render service C: Background Worker

Use the same commit and same database. Verify:

- `DATABASE_URL` is identical to the Web Service.
- `TRADING_MODE=paper`
- `LIVE_TRADING_ENABLED=false`
- `PAPER_SIGNAL_SOURCE=hybrid` if confirmed by the current design.
- `KALSHI_CREDENTIALS_ENCRYPTION_KEY` is identical to the Web Service.
- Do not copy your OpenAI API key to the worker; it is used by the separate provider service.

## Connect Kalshi once in dashboard

1. In Kalshi, create an API key in your account settings, download the matching private `.key`/`.pem` file, and use a demo key for demo validation first.
2. On the dashboard, unlock admin settings with `KALSHI_VAULT_ADMIN_SECRET`.
3. Choose **Demo** for test credentials; enter the API Key ID, select the matching private-key file, acknowledge the authorization statement, and click Connect.
4. Run only the read-only verify step first. The app stores the key encrypted; the private key is not returned to the browser.
5. The demo and production credentials are separate. Never upload a production key to an untrusted third-party service. The credential vault can store/verify keys but does not, by itself, enable live order submission.

## Automatic scanning and paper research

The scanner automatically inventories public markets with page/record caps. It is not literally unlimited and will report partial scans if a cap or upstream issue is reached. The deep research scheduler analyzes a smaller activity-ranked subset each cycle. It evaluates model probability, cited/fresh event evidence, liquidity/depth, spread, expected fee/slippage, net edge, exact ticker binding, category permissions, and independent calibration.

Prediction outcomes are collected only after the forecast has aged and the market has a real YES/NO resolution. Known resolution timestamps that precede the prediction are rejected. Until there are at least 100 subsequently resolved forecasts for the exact category/model/version, both YES and NO outcomes are adequately represented, and Brier score is at or below 0.22, the engine must not create research paper entries for that model/category. This warm-up is expected; don't falsify the validation registry to make it trade.

Once the provider responds correctly, set `MARKET_AUTOPAPER_ENABLED=true` on the main service/worker if you want to enable the dashboard's autopilot switch. The **dashboard autopilot switch itself defaults OFF**. Sign in, toggle it ON to enable scheduled scanning and eligible paper entries; toggle it OFF to pause them. This switch does not activate live order execution.

## Historical replay of your actual previous trades

After deploying this update to a paper/staging environment, unlock the dashboard and visit:

```text
https://<your-bot-host>/api/diagnostics/replay-export
```

That admin-only endpoint downloads a ZIP of curated CSVs for the actual BTC trade journal, features, signal decisions/counterfactuals, market research decisions, prediction outcomes, and research-paper trades. It explicitly excludes private keys, credentials, environment variables, and raw webhook payloads.

Run locally:

```bash
python audit_historical_export.py btc_bot_historical_replay_export.zip
```

This produces JSON and Markdown reports with win rate and Wilson confidence interval, net P&L, profit factor, drawdown, cohorts by entry price/signal source/score, explicit directional correctness where final outcomes are known, separate counterfactual counts, and model Brier scores. It is an audit of recorded choices, not a complete historical counterfactual replay if old quote/news snapshots were never retained. Do not upload API credentials with the export.

## Real order adapter status

`kalshi_order_adapter.py` contains a documented V2 signed limit-order adapter with demo-only default, fixed-point prices, client order IDs, hard caps, and no POST retries on ambiguous timeouts. It has mocked unit coverage. The app-level trading-mode gate intentionally keeps `REAL_ORDER_ADAPTER_INSTALLED=False` and live execution unavailable until a full Demo order lifecycle is tested (fill/remainder, reconciliation after timeout/restart, cancellation, fee and position reconciliation, emergency pause, and actual current API schemas). Do not set `LIVE_TRADING_ENABLED=true` or `LIVE_TRADING_APPROVED=true` to bypass this. A future real-trading toggle must be an explicit user action after those checks, separate from the research autopilot.

## Long validation soak

The bundled `run_validation_soak.py` defaults to **7,200 seconds of cumulative actual test-command runtime** (two hours), not sleep time, and strips API credentials from child tests. Run from the project root:

```bash
python run_validation_soak.py
```

The run is only complete if `validation_soak_result.json` reports `status=passed` and `completed_test_command_seconds >= 7200`. This suite is offline/mock testing; a separate, explicitly scheduled Demo-market validation is still required.
