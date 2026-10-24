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
- Adaptive-policy evaluations use a high-water mark for newer hybrid trade IDs, so backfilling old settlements cannot repeatedly tighten the entry target against the same sample.

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
- The final dashboard UI update places Realized P&L, Engine Connection, and Risk Limits in the very first content row immediately below the global header and before the intro heading; the Active Paper Position card follows the intro. It separates current 15-minute window status, latest directional evaluation, and currently open position. A previous direction stays visible only with a historical/expired label; the active position preserves its entry-linked rationale and warnings when the window status changes to WAIT.
- GitHub Actions compiles the repository, runs each test module in isolated processes, checks category/security/analytics tests, and runs an offline end-to-end smoke test.

## Phase 7 — Multi-category preparation (not live/active)
- Added a registry that lists source evidence and validation gaps for sports, politics, economics, weather, other financial markets, crypto-event, and entertainment contracts.
- Only the existing BTC 15-minute strategy is marked paper-enabled. Non-BTC categories remain research-only and are not connected to the worker's order/entry path. This is an architecture scaffold, not a completed or profitable strategy for those categories.

## Test/validation status
The final validation report accompanies this package. All repository test modules, including adaptive-policy, post-trade learning, and dashboard-context suites, are run separately to prevent unittest mocks from leaking across modules. Compile, workflow YAML checks, HTTP integration tests, database migration/idempotency tests, and the offline webhook-to-paper-entry smoke test are included.

## Deployment checklist
1. Use a branch or backup of `main` before applying the patch.
2. Do not include `.env`, production secrets, or a database file.
3. Confirm every GitHub Actions step passes.
4. Deploy the exact same commit to Web Service and Background Worker.
5. Verify `/health`, `/api/analytics`, `/api/decision-audit`, `/api/category-readiness`, and `/diagnostics/market-data`.
6. Keep paper mode and risk variables below unchanged throughout validation.
7. Do not enable live trading based on the small historical sample.


## Post-trade learning and current/next-signal dashboard update
- Added a `trade_learning` table and `trade_learning.py` to create one idempotent post-trade review per settled paper trade, including entry-time feature snapshots and realized P&L labels.
- Worker syncs and backfills settled trade outcomes in bounded batches each cycle, and can recover linked entry context through the external alert ID for legacy trades without `entry_signal_log_id`; `/api/learning` is read-only and never writes on dashboard polls.
- Adds cohort comparisons for chart-trend alignment, entry price, evidence score, volume and momentum, plus a bounded adaptive price-target adjustment. After at least 20 settled hybrid trades, the policy can tighten the preferred entry target by 1¢ only if at least 8 recent trades in the active high-price zone have negative net P&L of at least 8¢ and losses are at least as common as wins. It evaluates again only after 10 additional newer hybrid trade IDs, never loosens the target automatically, and tightens by no more than 5¢ below the original target. The 92¢ hard cap, trade budget, daily loss limit, and live-trading permission are not changed.
- Dashboard now places Realized P&L, Engine Connection, and Risk Limits first in the overview row, then the Active Paper Position card. It shows the effective adaptive target/status in the Risk Limits card and the learning panel. The “Waiting For Next Available Signal” panel retains its countdown and reasons; the historical direction remains where it was.

## Major-drop profit protection (paper-only)

After a paper position has reached and armed its fee-adjusted take-profit target, optional profit protection can trigger an immediate simulated exit when either (a) the internal engine detects a predefined major opposing setup, or (b) a newly closed 1-minute BTC candle moves adversely by at least `PAPER_TAKE_PROFIT_MAJOR_MOVE_ATR_MULTIPLIER` times the ATR baseline from prior closed candles. Configure `PAPER_TAKE_PROFIT_PROFIT_PROTECTION_ENABLED=true` and `PAPER_TAKE_PROFIT_MAJOR_MOVE_ATR_MULTIPLIER=2.5`. The exit is modeled from the observed Kalshi bid, subtracts entry and exit fees, and is logged as `PROFIT_PROTECTION`. This is best-effort only: gaps, wide spreads, missing liquidity, or polling delays can result in an exit below break-even; no no-loss outcome is guaranteed. It applies only to simulated paper positions and never enables real orders.

### Entry-price semantics update

`PAPER_ENTRY_TARGET_MAX_CENTS` (default 80¢) is a soft preferred price, not a trade blocker. A directional signal that passes freshness, major-flaw, quote, spread, risk, and budget checks may enter above that preference up to `PAPER_MAX_ENTRY_CENTS` (default hard cap 92¢). Quotes above the hard cap wait and are not opened. Higher entry prices leave less maximum upside; a direction/evidence score is not a calibrated win probability.


## Entry-price and decision-display correction
- The 80¢ preferred entry target is advisory, not a price gate. With the default 92¢ hard cap, a signal that otherwise passes freshness, hybrid major-flaw, executable quote/spread, window and risk/budget checks can enter from above 80¢ through 92¢. Above 92¢ it waits. This does not imply a positive expected value; expensive contracts leave little upside.
- Dashboard now distinguishes the soft preferred target from the hard cap and falls back to saved chart evidence when a WAIT row stores a zero action score, preventing contradictory “0/100” display next to a nonzero threshold reason.
- Premium-entry explanation/warning is persisted with the signal payload so the dashboard can show why the entry was allowed above the preference.


## Phase 1–3 context, validation, and history update
- The continuous BTC trend monitor labels UTC weekday/weekend, six-hour UTC time block, descriptive trend/range state, and the signal engine's existing volatility chase-warning status. These labels are descriptive only and do not change signal weights or approve entries.
- The per-trade journal now persists matching calendar and regime labels and groups outcomes by these contexts. Missing context remains `unknown`, and the raw three-candle move is not treated as a calibrated volatility regime.
- Added a chronological 70/30 context holdout review. It requires at least 30 settled trades and minimum cohort samples in both slices; output is review-only and cannot authorize automatic signal-weight changes.
- Added append-only `adaptive_policy_history` records for preferred-entry-target reductions and surfaced them in the read-only learning API/dashboard. The policy still cannot increase its target or change the hard cap, dollar budget, daily loss limit, or live-trading permissions.
- Latest dashboard hotfix remains applied on top of the master patch; the current-window, alert-ID linkage, display-warning, and Web Service/worker signal-source diagnostics are preserved.


## 2026-10-24 — Multi-market research-to-paper pipeline and validation improvements

- Added `market_decision_engine.py`, `market_analysis_provider.py`, and `market_decision_pipeline.py` for a separate research-to-paper path. It requires a configured external HTTPS probability/evidence provider and never submits live orders.
- Added per-category current evidence checks, exact market-ticker binding, model freshness, out-of-sample sample-count/Brier checks, liquidity/spread/edge checks, hard fee/slippage assumptions, and separate research paper journal/caps. Unknown categories and models cannot qualify.
- Added dashboard research batch/performance view, a confidence interval for observed win rate, property-stress test, and a cumulative actual test-command-time soak runner.
- Automatic research paper entries remain disabled by default until the operator validates/configures a provider. Live order execution and complete production multi-user isolation are still not implemented.
