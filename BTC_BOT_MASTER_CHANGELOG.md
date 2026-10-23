# BTC Bot Master Upgrade — Change Log

## Purpose
Consolidate reliability fixes and the Phase 3–6 research/analytics work against the complete repository supplied by the user, with a guarded Phase 7 category-readiness scaffold. The change set preserves the BTC-only, paper-only execution model.

## Reliability and data integrity
- Webhook payloads are size-limited and recursively scrubbed of sensitive-looking fields before persistence/exposure.
- Webhook delivery fingerprints are enforced with an idempotent partial unique index. The migration preserves historical alert rows and clears duplicate fingerprints from later rows rather than deleting audit history.
- Manual write endpoints are disabled unless `PAPER_ADMIN_API_SECRET` is set; callers must then supply `X-Paper-Admin-Secret`. This does not affect the signed webhook or automatic paper worker.
- WAIT decisions preserve the computed evidence score, while keeping WAIT as the decision when the configured threshold is not met.
- When market discovery or an order book is temporarily unavailable, the worker logs why it is waiting and retains a retryable pending alert where appropriate.
- Dashboard analytics operate on the latest 10,000 trade and signal records rather than accidentally analyzing the oldest 10,000 after the journal grows.
- FastAPI startup uses a lifespan handler to avoid deprecated startup-event usage.

## Phase 3 — Decision audit and outcomes
- A `decision_audit` stream records decision-related events and includes reason/context fields for post-hoc review.
- Existing signal and paper-trade rows remain the source of actual realized P&L; rejected-signal counterfactuals are explicitly hypothetical and never counted as paper trades.
- Payload filtering and duplicate suppression protect the audit trail from accidentally storing credentials or enqueuing the same timestamped webhook twice.

## Phase 4 — Performance analytics
- The analytics endpoint reports strategy groups, outcomes, net P&L, rolling windows, entry-price cohorts, drawdown/profit-factor summaries, counterfactuals, and shadow-exit comparisons already available from the saved journal.
- Reports label sample sizes and do not treat market-implied prices or heuristic evidence scores as calibrated win probabilities.
- Analytics calculations are read-only and analyze the newest capped window for bounded memory/use.

## Phase 5 — Exit and rejected-signal review
- Existing exit handling remains unchanged; the package does not tune stop-loss or take-profit rules to force favorable test outcomes.
- Shadow exits and rejected-signal outcomes remain clearly marked hypothetical and are reported separately from actual trade results.
- Retryable WAIT states due to missing market or order-book data are now represented in the decision trail.

## Phase 6 — Dashboard, APIs, and operations
- Read-only `/api/analytics`, `/api/decision-audit` and `/api/category-readiness` surfaces support observation and analysis. The dashboard API separately returns `latest_directional_signal` and the open `active_position` linked to its saved entry signal.
- Manual journal/evaluation write routes are fail-closed when the optional admin secret is unset.
- The final dashboard UI update separates current 15-minute window status, latest directional evaluation, and currently open position. A previous direction stays visible only with a historical/expired label; the active position preserves its entry-linked rationale and warnings when the window status changes to WAIT.
- GitHub Actions compiles the repository, runs each test module in isolated processes, checks category/security/analytics tests, and runs an offline end-to-end smoke test.

## Phase 7 — Multi-category preparation (not live/active)
- Added a registry that lists source evidence and validation gaps for sports, politics, economics, weather, other financial markets, crypto-event, and entertainment contracts.
- Only the existing BTC 15-minute strategy is marked paper-enabled. Non-BTC categories remain research-only and are not connected to the worker's order/entry path. This is an architecture scaffold, not a completed or profitable strategy for those categories.

## Test/validation status
The final validation report accompanies this package. All 26 available repository test modules (101 test cases) are run separately to prevent unittest mocks from leaking across modules. Compile, workflow YAML checks, the HTTP integration tests, database migration/idempotency tests, and the offline webhook-to-paper-entry smoke test are included.

## Deployment checklist
1. Use a branch or backup of `main` before applying the patch.
2. Do not include `.env`, production secrets, or a database file.
3. Confirm every GitHub Actions step passes.
4. Deploy the exact same commit to Web Service and Background Worker.
5. Verify `/health`, `/api/analytics`, `/api/decision-audit`, `/api/category-readiness`, and `/diagnostics/market-data`.
6. Keep paper mode and risk variables below unchanged throughout validation.
7. Do not enable live trading based on the small historical sample.
