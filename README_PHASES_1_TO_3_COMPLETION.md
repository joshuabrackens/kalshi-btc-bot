# BTC Bot Phases 1–3 — Completed Local Upgrade

This bundle is based on the saved BTC bot project snapshot, the latest master/soft-entry patch, and the latest current-alert dashboard hotfix. It remains BTC-only and paper-only. No live-order route or permission is enabled.

## Phase 1 — Daily/weekend adaptation and market context

- The continuous trend monitor now labels the current UTC weekday/weekend and six-hour time bucket alongside the 1m/5m/15m signal context.
- It reports descriptive trend/range and existing chase-warning state labels from saved timeframe, box-theory, market-structure, and volatility evidence.
- Each settled paper trade's learning snapshot records the UTC day type, weekday, time bucket, trend/range label, and whether the existing volatility warning was raised.
- `/api/learning` compares realized outcomes by weekday/weekend, weekday, UTC time bucket, trend regime, and volatility-warning state. Its cohort table is visible in the dashboard.
- **Signal weights are not automatically changed.** Raw move percentage is not called high/low volatility without a calibrated baseline. The labels are descriptive, and context cohorts remain research leads.

## Phase 2 — Learning quality and controlled adjustment

- The existing per-settled-trade journal continues to store an immutable entry-time feature snapshot and fee-inclusive realized net P&L once per trade.
- A chronological 70/30 walk-forward context review uses older settlements as the discovery slice and the newest 30% as a holdout. It refuses to draw a conclusion below 30 total settled trades and also requires minimum per-cohort counts in both slices.
- Walk-forward output is review-only and explicitly does not authorize signal-weight changes.
- The existing bounded paper-only preferred-entry policy still cannot raise the preferred target, hard cap, trade budget, daily loss limit, or live-trading permission. Each downward target change is now recorded in the append-only `adaptive_policy_history` table and exposed by `/api/learning` and the dashboard, so a change can be reviewed and manually reversed if later outcomes justify it.
- Small or missing historical samples are labeled insufficient/unknown rather than guessed.

## Phase 3 — Dashboard and engine stabilization

- Includes the latest current-alert/display-warning hotfix: `alreadyEvaluated` is declared, exact external alert IDs are linked to evaluations when available, terminal queued/skipped/vetoed/processed states are shown transparently, and Web Service versus worker signal-source mismatches are exposed.
- Dashboard keeps Realized P&L, Engine Connection, and Risk Limits in the first row; current-window WAIT state, prior directional context, and active position remain distinct.
- The continuous monitor displays current UTC session context and the descriptive trend/volatility labels.
- Existing tests cover paper-only fail-closed behavior, fresh-signal/wait states, order-book-derived prices, worker/shared-queue flow, stop-loss/trailing take-profit/profit protection, webhook idempotency, DB migrations, dashboard context, HTTP/API safety, and trade learning.

## Validation

The isolated local matrix reports 28 test modules / 124 test cases passing. See `BTC_BOT_PHASE123_VALIDATION.md` for the module-by-module summary, compile result, dashboard checks, and offline smoke-test result. GitHub Actions still needs to pass after these files are committed.

Passing offline tests does not prove the production Render Web Service and Background Worker share the same PostgreSQL database or that a production webhook reaches the live worker. Verify the deployed heartbeat, processing status, worker signal source, and shared `DATABASE_URL` after deployment.

## Safety / deployment

Keep both Render services on the same shared PostgreSQL `DATABASE_URL`, with `PAPER_SIGNAL_SOURCE=hybrid`, `TRADING_MODE=paper`, `LIVE_TRADING_ENABLED=false`, and the currently reviewed entry/risk values. Keep `PAPER_WORKER_ENABLED=false` on the Web Service when a separate Background Worker runs `paper_worker.py`. Do not paste secrets into GitHub, screenshots, or chat. Do not enable live trading based on this validation package.
