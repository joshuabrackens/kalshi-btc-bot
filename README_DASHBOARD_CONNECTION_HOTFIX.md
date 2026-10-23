# Dashboard Connection Hotfix

## Cause
The `/api/dashboard` route decorator was accidentally placed on the `_dashboard_signal_from_row(row)` helper instead of on `dashboard_data()`. FastAPI therefore registered a route that required a `row` request parameter and returned HTTP 422 to the dashboard, causing the UI to display “Dashboard connection issue.”

## Files
- Replace `main.py` at the repository root.
- Replace `test_app_http_surface.py` at the repository root. This adds an HTTP regression assertion that `/api/dashboard` returns HTTP 200 and includes `current_window_decision` and `active_position`.

## Validation
Locally verified:
- `GET /api/dashboard` returns HTTP 200 JSON.
- Response includes `mode=SIMULATION_ONLY`, `current_window_decision`, and `active_position`.
- `test_app_http_surface.py`, `test_dashboard_context.py`, and `test_analytics_api.py`: 5 tests passed.
- Python compilation passed.

This patch does not change trading rules or require environment changes. Keep paper mode and live trading disabled.
