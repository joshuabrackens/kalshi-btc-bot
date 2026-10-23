# Dashboard connection-state diagnostic hotfix

## What this fixes

The dashboard was showing a red "Dashboard connection issue" label even while its engine/health panel had refreshed. The old refresh routine wrapped all panel rendering in one broad `try/catch`, so any one rendering exception was incorrectly reported as an API connection failure and hid the failing section.

This patch separates three conditions:
- `/api/dashboard` failed: the connection badge remains red and the footer displays the actual request error.
- `/api/dashboard` succeeded but a display panel threw: the badge says `API connected · display warning (N)` and the footer names the panel(s) that failed. Browser console includes the stack trace.
- API and panel rendering succeeded: the badge says `Dashboard connected`.

It also makes signal-reason rendering robust to malformed/non-array `reasons` values. No trade rules, database schema, secrets, or environment variables change.

## Files

Replace at repository root:
- `dashboard.html`
- `test_dashboard_context.py`

## Apply and verify

1. Replace both files in GitHub (keep the existing path at root).
2. Commit as `Improve dashboard connection diagnostics`.
3. Wait for all GitHub Actions steps to pass.
4. Let the Web Service deploy that commit. The Background Worker does not need a code restart specifically for this frontend-only change, but keeping both services on the same commit is preferred.
5. Hard-refresh the dashboard with `Ctrl+F5`.
6. Read the top badge and footer message. If a panel still fails, the footer will now say which section failed. Press `F12` → Console for the detailed error.

Keep `TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false` unchanged.
