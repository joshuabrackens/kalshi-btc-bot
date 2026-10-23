# BTC Engine dashboard redesign

This update replaces only `dashboard.html` and keeps the current server API intact.

## What changed

- Matches the reference photos' navy/blue gradient, bright blue panels, rounded cards, status pills, compact labels, and typography.
- Preserves the engine overview, realized P&L, risk limits, signal explanation, strategy checks, trade history, and alert history.
- Adds worker heartbeat/action/reason and a live `/diagnostics/market-data` section for the BTC candles and Kalshi order book.
- Displays the evidence score separately from probability measures.
- Shows model win probability as **Not calibrated** when the API does not provide a calibrated probability. It also shows (when available) a rough market-implied estimate from the entry ask and the overall historical win rate, each with a clear explanation.
- Keeps the dashboard read-only and labels simulation-only mode prominently.

## Apply the update

1. In the GitHub repository root, open `dashboard.html`.
2. Replace its contents with the included `dashboard.html` file (or upload the file with the same name to the repository root).
3. Commit as `Refresh BTC Engine dashboard design`.
4. Let the Render Web Service deploy, then open the dashboard and hard-refresh with `Ctrl+F5`.

No Python changes or Render environment-variable changes are required for this UI update. The page reads `/api/dashboard` and `/diagnostics/market-data`, both of which are already part of the current app.

Keep `TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false` unchanged.
