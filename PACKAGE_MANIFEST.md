# Master bot package manifest

This is the complete repository based on the user-uploaded `main` branch ZIP, reconciled with reliability, audit, analytics, per-trade feedback, category-readiness, test, and dashboard changes in this package. The master patch ZIP contains **39 files: 26 replacements and 13 additions** relative to that uploaded source ZIP. It preserves repository-relative paths.

## Modified existing files
- `.github/workflows/python-app.yml` — active GitHub Actions workflow; isolated full test matrix and offline smoke test.
- `python-app.yml` — kept in sync with active workflow (historical duplicate at repository root).
- `README.md` — current source-of-truth overview, configuration, operations, safety, and deployment guide.
- `README_PHASE3_TO_6_UPGRADE.md` — current upgrade/deployment guide for analytics, audit, security, and UI changes.
- `README_PHASE1_MARKET_DATA.md` — retained as a historical note pointing to the current master package instructions.
- `db_store.py` — append-only decision audit schema/indexes, idempotent webhook-fingerprint migration, persistent `trade_learning` outcome journal, and migration-safe `adaptive_policy_state` watermark/schema.
- `env.example` — safe configuration examples and current entry/risk settings.
- `requirements_additions.txt` — clarified as a historical note; `requirements.txt` remains the sole runtime dependency source.
- `main.py` — analytics/audit APIs, category-readiness, webhook-size limit, secret redaction, idempotent webhook insert, guarded manual write APIs, newest directional-signal and linked open-position context for the dashboard, and lifespan cleanup.
- `paper_worker.py` — enriched WAIT reasons/audits, robust retry statuses, idempotent post-settlement learning sync, and use of the persisted adaptive entry target in hybrid paper-mode checks.
- `signal_engine.py` — preserve calculated evidence score on a WAIT/below-threshold result.
- `smoke_shared_alert_flow.py` — offline webhook → shared queue → hybrid decision → paper-trade smoke test.
- `dashboard.html` — final dashboard with Realized P&L, Engine connection, and Risk limits at the top, followed by Active paper position; current WAIT state, historical direction, and active-position entry rationale remain separate.
- `test_hybrid_take_profit.py` — align tests with current DB interface and order-book-derived executable quotes; keep behavior checks intact.
- `test_paper_bot_fixes.py` — regression test for WAIT-score preservation.
- `test_paper_worker.py` — test isolation, current worker interface, and network-free expectations.
- `test_trend_monitor_worker.py` — deterministic feed stub to prevent accidental network access.
- `test_wait_alerts.py` — unit-test isolation for worker monitor side effects.

## Added source and test files
- `.gitignore` — ignores local database, bytecode, caches, virtual environments, and `.env` files.
- `payload_safety.py` — dependency-free recursive sensitive-key redaction.
- `market_category_registry.py` — read-only category readiness scaffold; only the existing BTC 15-minute flow is paper-enabled.
- `trade_learning.py` — per-settled-trade feature snapshots, realized outcome labels, cohort analysis, and the bounded one-way adaptive entry-target policy.
- `test_trade_learning.py` — idempotency, settlement gating, realized-vs-exit-reason correctness, and cohort-report tests.
- `test_app_http_surface.py` — HTTP-level safety/auth/analytics/category API integration tests including read-only learning endpoint and post-settlement capture.
- `test_dashboard_context.py` — API and HTML regression coverage for historical directional context and open-position rationale while the current window shows WAIT.
- `test_category_registry.py` — category classification and fail-closed tests.
- `test_manual_api_security.py` — explicit-secret/disabled-by-default write API security tests.
- `test_payload_safety.py` — payload redaction, nesting, and request-size tests.
- `test_webhook_idempotency.py` — duplicate fingerprint migration and unique-index tests.
- `BTC_BOT_MASTER_CHANGELOG.md` — change summary, validation scope, and deployment checklist.
- `BTC_BOT_VALIDATION_RESULTS.md` — local test and smoke-test results.

## Per-trade feedback / learning system
`trade_learning.py` writes one idempotent feature snapshot and realized P&L review for each settled paper trade. Worker cycles backfill older settled trades in bounded batches; `/api/learning` is read-only. Legacy trades can fall back to matching an external alert ID when direct signal-log linkage is absent. A persistent, one-way adaptive policy may tighten the preferred entry target after at least 20 settled hybrid trades when at least 8 trades in the active high-price zone have at least 8¢ net loss and losses are at least as numerous as wins. It changes by 1¢ per qualifying review and evaluates after each 10 additional newer hybrid trade IDs; a watermark prevents old backfill from repeating the same adjustment. Tightening is capped at 5¢ below the original target, and the hard cap/dollar limits/live permissions never change.

## Final dashboard change
`dashboard.html` is updated in the final UI pass so Realized P&L, Engine Connection, and Risk Limits occupy the first content row immediately below the global header and before the intro heading; the Active Paper Position card follows the intro immediately. Current-window waiting, the last directional evaluation, and active-position context stay separate. The current/next-signal panel says “Waiting For Next Available Signal” when no position is open, shows the response/window countdown and waiting reasons, and the learning panel shows outcomes, cohorts, the effective target, and the last adaptation reason. The active position keeps its linked entry-signal rationale even if the current-window indicator state later falls back to `WAIT THIS WINDOW`.

## Existing project files retained
The package retains the repository's existing `candle_feed.py`, `trend_stop.py`, `market_discovery.py`, `kalshi_api.py`, `signal_fusion.py`, `btc_trend_monitor.py`, performance-analysis modules, migration script, existing tests, requirements, and Dockerfile. They are not replaced merely for packaging convenience.

## Safety constraints
- `TRADING_MODE=paper`
- `LIVE_TRADING_ENABLED=false`
- `PAPER_MAX_ENTRY_CENTS=92`
- `PAPER_ENTRY_TARGET_MAX_CENTS=80`
- `MAX_TRADE_DOLLARS=1.00`
- `MAX_DAILY_LOSS_DOLLARS=10.00`
- No secrets, production environment files, or local database are included. `PAPER_ADAPTIVE_ENTRY_TARGET_ENABLED` is optional (default `true`); set it to `false` on the Background Worker to disable the bounded entry-target adaptation. If you override it, keep the setting consistent with the Web Service so the displayed policy status matches worker behavior.
