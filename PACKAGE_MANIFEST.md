# BTC Bot — current all-phases source bundle

Snapshot: October 24, 2026 UTC. This source bundle contains the BTC 15-minute paper strategy, trend/context and bounded learning systems, dashboard, credential vault, market scanner, automated market research-to-paper pipeline, analysis provider service source, calibration/outcome collector, historical export/audit tools, Demo-only low-level order adapter, tests, workflows, soak runner, setup documentation and win-rate evaluation plan.

## Included scope

- Phases 1–3: context cohorts, bounded learning, current-window/signal handling, trend stops, paper profit protection, paper-worker reliability, idempotency and analytics.
- Phase 4: bounded cursor-paginated market inventory; activity-ranked rotating research; current market data, source-attributed evidence, probability-vs-price, liquidity/depth/spread, estimated fees/slippage, net-edge/model-validation/category/risk gates, separate paper research journal and resolved-outcome calibration.
- Phase 5 foundations: session/security helpers and readiness checks; complete production multi-user tenancy is not yet finished.
- Phase 6: encrypted credential vault UI and Paper/Real mode selector; low-level signed order adapter for isolated Demo testing only. It is not wired into the application live-execution path; Real remains blocked.
- Provider: source `market_analysis_provider_service.py` is included but must be deployed separately as HTTPS with an account-owned `OPENAI_API_KEY`, enabled Responses API model, the generated shared bearer token, and shared PostgreSQL for calibration.
- Audit: admin-only `/api/diagnostics/replay-export` produces curated CSVs; `audit_historical_export.py` examines actual recorded outcomes and known decisions without inventing missing historical quotes/news.

## Safe settings

- `TRADING_MODE=paper`
- `LIVE_TRADING_ENABLED=false`
- `LIVE_TRADING_APPROVED=false`
- `MARKET_LIVE_EXECUTION_ENABLED=false`
- `MARKET_AUTOPAPER_ENABLED=false` until the provider and its evidence/calibration are validated; dashboard master autopilot defaults off.

## Validation

The latest local suite passed 215 tests, with Python compilation, dashboard JavaScript syntax, market decision property stress, phase 4–6 readiness stress, browser mock flow, and offline webhook-to-paper smoke tests passing. The requested 7,200 seconds of cumulative actual test-command runtime has not been completed; see `BTC_BOT_MARKET_PIPELINE_VALIDATION.md` and run `python run_validation_soak.py` in a suitable long-lived environment.

## Secrets and hygiene

The source ZIP excludes `.env`, actual Kalshi private keys, generated app secrets, local SQLite journals, caches and compiled bytecode. `env.example` contains placeholders only. The application-side secret file is delivered separately; vendor API keys must be created in the user's own provider accounts.
