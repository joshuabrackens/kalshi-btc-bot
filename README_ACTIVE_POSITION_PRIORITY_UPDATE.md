# Dashboard active-position priority update

## What changed
- Moved **Active paper position** directly below the page intro and above Engine Overview and the latest directional evaluation.
- Increased the active-position card border emphasis so an open position is easier to spot immediately.
- Preserved the separation between current 15-minute window status, last directional evaluation, and the exact rationale for an open position.
- Added a tooltip/accessibility label to the header display-warning badge that identifies the panel(s) that failed to render. The detailed refresh footer and browser console remain available for diagnosis.

## Files
Replace these files in the repository root:
- `dashboard.html`
- `test_dashboard_context.py`

No environment variables, database schema, or trading logic changes are required. This is a dashboard-only patch.

## Verification
Local checks performed:
- `python -m pytest -q test_dashboard_context.py test_app_http_surface.py` — 5 passed.
- Python HTML parser verified 175 unique element IDs and that the active-position section is placed after the intro and before Engine Overview and latest directional evaluation.
- `node --check` passed for the dashboard's inline JavaScript.

After commit, wait for GitHub Actions to pass and let the Render Web Service redeploy. A hard refresh (Ctrl+F5) loads the new dashboard.
