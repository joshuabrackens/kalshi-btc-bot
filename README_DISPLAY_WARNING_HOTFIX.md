# BTC Bot — Dashboard Display Warning + Current Alert Hotfix

This small patch is for the already-deployed Master Patch. It is **not a full repository replacement** and does not change trading strategy, risk limits, or live-order permissions.

## Replace exactly these 4 repository-root files

- `dashboard.html`
- `main.py`
- `test_current_signal.py`
- `test_dashboard_context.py`

Keep all files in the repository root. Commit to GitHub and wait for GitHub Actions to pass, then deploy the same commit to the Render **Web Service**. This patch does not modify `paper_worker.py`, so the worker code itself does not need replacing for this display/API hotfix.

## What was fixed

1. **Display warning:** `renderNextSignalWaiting()` referenced `alreadyEvaluated` without declaring it. The resulting JavaScript ReferenceError can cause the dashboard's `API connected · display warning (1)` badge.
2. **Current alert context:** `/api/dashboard` now exposes the current alert ID, processed timestamp, processing status, and processing note from the shared alert journal.
3. **Current evaluation matching:** the dashboard matches a saved evaluation to its exact external alert ID when available. This includes cases where the recorded action is `WAIT` but the proposed UP/DOWN direction is still present, such as a veto or quote-related wait.
4. **Transparent waiting states:** the dashboard distinguishes a queued alert, waiting-for-market, waiting-for-order-book, and a terminal processing status. It should no longer say “evaluation pending” indefinitely if the worker has already marked the alert skipped/vetoed/processed but there's no linked detailed evaluation row.
5. **Worker configuration diagnosis:** the API exposes `worker_signal_source` from the worker heartbeat. The dashboard displays a clear mismatch if the Web Service is configured as `hybrid` but the worker heartbeat reports `internal` (or vice versa).
6. **Regression tests:** add checks for alert processing-note propagation, external alert ID retention, and the `alreadyEvaluated` declaration.

## Validation

- Full Master Patch suite previously passed in isolated module processes: 27 modules, 119 cases.
- After these final changes, focused current-signal/dashboard-context tests passed (10 tests); current-window tests passed (4); HTTP surface tests passed (2); wait-alert tests passed (2); webhook idempotency passed (1).
- Offline shared-alert-flow smoke test passed: webhook receipt → shared queue → hybrid decision → simulated paper entry → dashboard heartbeat.
- Python compilation, dashboard JavaScript syntax, and HTML ID validation passed (204 IDs, zero duplicates).

These checks are offline. They do not directly prove that the production Web Service and Background Worker share the same PostgreSQL `DATABASE_URL`. After deployment, check the dashboard's displayed worker source and the latest alert processing status. Keep `TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false`.
