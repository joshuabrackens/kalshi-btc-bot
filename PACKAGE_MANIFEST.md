# Current BTC Bot Project Package Manifest

Release snapshot prepared 2026-10-24 05:38 UTC.

**Project file count:** 121 eligible files (listed in `PHASE1_TO_6_CURRENT_MANIFEST.txt`).

The project includes the original BTC 15-minute paper engine, UTC and regime context, settled-trade learning and outcome reporting, dashboard and admin controls, encrypted credential-vault support, standard and multivariate Kalshi catalog scanner, research decision/paper journal, HTTPS evidence/probability provider, current orderbook fixed-point normalization, tests, workflows, diagnostics, historical audit tooling and soak runner.

Security exclusions: no `.env` values, account credentials, API private keys, app-secrets file, local SQLite/PostgreSQL journal, cache directories, Python bytecode or runtime test logs are included.

Current validation evidence is in `BTC_BOT_MARKET_PIPELINE_VALIDATION.md`. The requested two-hour soak, post-patch Render verification and replay of the user's actual production journal have not been completed here.

The scanner traverses both `GET /events` and `GET /events/multivariate`, within bounded caps. Deep analysis rotates through an activity-ranked pool. The dashboard autopilot can create isolated research-paper entries only after provider/authentication, category evidence, model calibration, current quotes, liquidity/edge and risk checks pass. Live order placement remains locked.
