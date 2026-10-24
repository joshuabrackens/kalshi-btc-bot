# BTC Bot all-phases validation report

**Snapshot:** October 24, 2026 UTC. This report applies to the local source snapshot only, not to any unverified live GitHub/Render deployment.

## Latest checks

- `python -m pytest -q`: **215 passed** (latest run: 2.00 seconds).
- `python -m compileall -q .`: passed.
- `python validate_dashboard_js.py`: passed.
- `python stress_market_decision_engine.py`: 25,000 deterministic market-decision cases passed; no uncaught exceptions and no orders submitted.
- `python stress_phase456_readiness.py`: passed 100,000 metadata cases, 20,000 mode-gate cases, 10,000 session-token uniqueness checks; market inventory remained capped at 500 records from a virtual 1,000,000.
- `python smoke_shared_alert_flow.py`: offline webhook -> shared queue -> hybrid decision -> simulated paper entry -> dashboard heartbeat passed; this was simulation-only.
- Browser mock test: API connect form present; credential verify/store flow passed using mock credentials; private-key input cleared after submit; autopilot toggle switched on/off; zero browser page/console errors.
- Historical export endpoint regression tests passed: admin protection works and curated exports exclude credentials and raw webhook payloads.
- `kalshi_order_adapter.py`: mock tests passed with temporary test keys; it was not tested against a real Kalshi Demo account or production endpoint.

## Current capabilities

- BTC strategy support: weekday/weekend and time-of-day cohorts, bounded learning history, signal/current-window dashboard handling, trend-stop/profit-protection behavior, paper worker, alert idempotency, analytics and decision audits.
- Market inventory: cursor-paginated public open-market discovery with page/record caps and explicit failure/partial status.
- Research-to-paper: activity-ranked rotating pool; current market data and order book; model probability vs market quote; source-attributed current evidence; liquidity/depth/spread; fee/slippage estimates; net edge; calibration and category/risk gates; isolated paper-research journal with deduplication and exposure/loss limits.
- Analysis provider: service source is included (`market_analysis_provider_service.py`) but must be deployed separately as HTTPS with an OpenAI API key owned by the user, a private shared bearer token, and access to the shared PostgreSQL database for calibration. The service reports evidence/probabilities, never execution permission.
- Actual outcome calibration: the project records predictions and later reads public YES/NO resolution after minimum age; it requires at least 100 resolved observations per model/category/version, adequate representation of both outcomes, and Brier <=0.22 before a category/model passes the calibration gate.
- Credential UX: screenshot-inspired Key ID + private-key file uploader + acknowledgment checkbox + encrypted server-side storage/read-only verification; application-generated secrets are separate from vendor credentials.
- Dashboard autopilot: controls periodic market research and eligible research paper entries only. It does not unlock live order execution.
- Historical review: protected `/api/diagnostics/replay-export` and `audit_historical_export.py` review actual stored decisions and outcomes. They cannot recreate historical quotes/news that were never archived.

## Not verified / blocked

- **One-/two-hour soak NOT completed.** Earlier attempts were interrupted at approximately 32 seconds of completed command runtime. `run_validation_soak.py` defaults to 7,200 seconds of actual offline test-command runtime; it must report `status=passed` and `completed_test_command_seconds >= 7200` before the requested two-hour soak can be claimed complete. No sleep padding is counted.
- Live Render state, actual Render Postgres, actual OpenAI provider credentials, and Kalshi Demo have not been exercised from this environment. Use staging, inspect logs, and run Demo lifecycle tests after deployment.
- Earlier production dashboard screenshot showed `/api/market-scan` returning 502. The local scanner now uses the documented API base by default and tracks progress/failure explicitly, but this does not prove the current deployed service's 502 is fixed. Check for a stale `KALSHI_BASE_URL` override and Render upstream/network errors.
- The user's ~95 paper trades / 56% win rate have not yet been analyzed from the actual production journal, because that Render database was not available to local tests. After deploying, sign into the dashboard and download `/api/diagnostics/replay-export`; run `python audit_historical_export.py btc_bot_historical_replay_export.zip`. Actual correctness replay is only possible for cases with known outcomes; full counterfactual replay requires historical quotes/evidence, which may not have been retained.
- The user must create their Kalshi API Key ID/private key and OpenAI API key in those accounts. The app-secret file only contains locally generated app-owned secrets.
- A low-level signed V2 order adapter exists with Demo-only defaults and mocked tests, but the app-level live-execution gate intentionally keeps `REAL_ORDER_ADAPTER_INSTALLED=false` and `LIVE_APPROVAL_RECORDED=false`. Live order placement is blocked. Keep `TRADING_MODE=paper`, `LIVE_TRADING_ENABLED=false`, `LIVE_TRADING_APPROVED=false`, and `MARKET_LIVE_EXECUTION_ENABLED=false`.
- Complete production multi-user login/middleware/tenant isolation is still unfinished.

## Win-rate interpretation

The reported baseline is approximately 95 paper trades at 56%. If that represents 53 wins out of 95 settled trades, the approximate 95% Wilson interval is 45.8%–65.4%; exact counts should be verified from the trade journal, excluding unsettled trades and distinguishing breakevens. Judge improvements by fee-adjusted expectancy, profit factor, drawdown, calibration, cohorts, and chronological out-of-sample tests—not win rate alone.

## Overall status

**Local regression/stress: PASS. Provider + live services: NOT VERIFIED. Production historical trade audit: PENDING EXPORT. One-/two-hour soak: NOT COMPLETED. Cross-market research autopaper: OFF by default pending provider and calibration validation. Live execution: BLOCKED.**
