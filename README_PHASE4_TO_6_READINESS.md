# Phases 4–6 Readiness Upgrade (Research-Only / Fail-Closed)

## Status at a glance

This update expands the Phase 1–3 project with a dashboard market scanner, stronger bounded-input and password-hashing controls, a paper/real mode request selector, explicit multi-user/live-readiness checks, a synthetic stress harness, and more regression tests. It does **not** complete production multi-market trading, turn on multi-user accounts, or add a real-order submission path.

| Phase | Current result | What is still needed |
|---|---|---|
| 4 — Multi-market | Read-only open-market catalog scanner; conservative category classification; bounded routing inventory; per-category evidence requirements. Only BTC 15-minute remains strategy-enabled. | Select one category, obtain/validate its primary data, define category-specific strategy and settlement rules, build historical backtests/calibration, then validate in separate paper mode before enabling any category. |
| 5 — Multi-user | Password/session primitives and an explicit disabled-readiness report. Public signup remains off. | Production login/logout, secure `HttpOnly`/`Secure`/`SameSite` cookies, CSRF middleware, session persistence/revocation, tenant-scoped database queries and webhooks, per-account risk reservations, login throttling, recovery/reset and audit processes, independent security review and cross-tenant tests. |
| 6 — Real trading | A visible Paper/Real selector and read-only readiness checklist. Real selection is rejected server-side. The worker remains paper-only and there is no real order adapter. | Complete auth, order API signing, pre-trade gates, idempotency, exchange-side order groups/cancel controls, fill/position reconciliation, fees/slippage, emergency stop and recovery; test end-to-end in Kalshi Demo, then run a documented approval/canary process. A separate explicit go-live decision is still required. |

## Dashboard mode selector

The Paper button confirms the server's effective paper-only state. Selecting Real sends a request to the server-side readiness gate; the API responds with HTTP 409 while blockers exist. It does not change deployment environment variables, select a production trading account, or place orders. This is intentionally different from a functioning live mode switch: no authenticated order adapter exists in this project snapshot. Do not set `TRADING_MODE=real` or `LIVE_TRADING_ENABLED=true`; the worker is designed to refuse that configuration.

## Phase 4 multi-market scanner

The dashboard's **Scan open markets** button calls `GET /api/market-scan`. The endpoint caches a successful public scan for 30 seconds by default to prevent repeated button clicks from spamming the upstream API, follows at most five 100-market cursor pages, consumes at most 500 returned records, stops if a cursor repeats, and the dashboard displays at most 50. It classifies validated BTC 15-minute markets to the existing BTC paper strategy, labels known other categories `research_only`, and blocks unknowns. No scan route creates orders, opens simulated positions, or feeds non-BTC contracts into the BTC signal engine. Pagination is reported, not silently treated as a complete scan.

A category must not be enabled based on its label alone. Each strategy needs a definition of the contract question/resolution source, independent data provenance and freshness, target probability calibration, historical and walk-forward tests, fee-adjusted outcomes, risk model, stale-data handling, and settlement reconciliation. Time-series validation must preserve chronology rather than shuffle future observations into training; scikit-learn explicitly warns that ordinary cross-validation can train on future data and test on the past. Also, strategy-parameter searches increase overfitting risk, so the observed winner of a backtest should not be called the best strategy without controls for selection bias.

## Phase 5 security baseline

`scrypt` password hash/verify helpers use random salts and constant-time digest comparison. Session tokens are generated with a cryptographically secure RNG; callers receive a token while the recommended persistence value is a one-way digest. Input length/salt constraints prevent avoidable over-sized input work. These are **building blocks, not an account system**: no login routes/middleware, persistent session lifecycle, user-to-trade ownership model, tenant-scoped workers, or account isolation is wired. Keep the multi-user endpoint disabled until these exist and are tested end-to-end. OWASP recommends opaque unpredictable session IDs, secure cookie controls (`Secure`, `HttpOnly`, `SameSite`), server-side expiry/revocation, and preventing tokens from being exposed through browser storage/logs.

## Phase 6 live-readiness research

The current Kalshi docs identify an authenticated Trade API and a Create Order V2 endpoint at `POST /trade-api/v2/portfolio/events/orders`; modern integrations should follow the documented V2 shape rather than blindly reusing a legacy order endpoint. Authenticated requests require an API key ID, millisecond timestamp and signature. The documented signing process signs timestamp + HTTP method + path (without query parameters); RSA keys use RSA-PSS/SHA-256 and Ed25519 keys sign directly. A credential never belongs in the browser or repository.

A future adapter must make order submission idempotent with a unique client order ID; record every intent before dispatch; reconcile after timeouts instead of blind resubmission; handle partial fills, rejects, cancellations, pause behavior, resting orders and restarts; and verify actual fills/positions/settlements against its journal. It must use current market-specific fees and expected execution costs, and track exchange read/write rate budgets. A local status flag is not a sufficient emergency stop: outstanding exchange orders and open positions must be reconciled after stopping. An end-to-end order/fill/cancel/settlement cycle must be completed in Kalshi Demo before any separate decision to run a canary.

First end-to-end order tests should use Kalshi Demo with separate credentials and mock funds. Kalshi notes demo prices/behavior may differ from production. Demo is a functional-integration test, not evidence that real slippage, liquidity or profit expectations are validated.

## Additional improvements in this revision

- Market scanning now bounds **input consumption** with a lazy iterator cap as well as capping response size; an unbounded generator is not materialized just to slice its first 500 rows. A short server cache limits repeated upstream catalog calls from the publicly reachable scanner endpoint.
- Scanner pagination now rejects non-text and overlong cursors before issuing another upstream request, detects repeated cursors, and reports an incomplete sample clearly.
- Public market metadata is accepted only from string fields and truncated before normalization; malformed nested values do not get stringified into a plausible market identifier.
- Password hashing now caps password and salt size, strictly validates base64url fields, rejects malformed persisted digest lengths before expensive hash work, and treats malformed records as invalid without raising. Any populated session revocation field invalidates the session, even if its timestamp is malformed or in the future.
- Dashboard market scanning is manual, transparent about its sample/cursor, reports each route decision, and uses `textContent` for untrusted market fields.
- Stress harness runs without network, credentials, exchange actions, or production DB writes: 100,000 synthetic market metadata fuzz cases, 20,000 mode-gate checks, 10,000 session token uniqueness/digest checks, and a one-million-item virtual market feed whose consumption must cap at 500. Cache tests cover repeated reads, TTL expiry, concurrent scan requests, failed-fetch recovery, repeated cursors, non-text cursors, and cursor length bounds.
- Every live readiness result is fail-closed. All non-BTC strategy profiles remain disabled.

## Official research references (checked 2026-10-23)

- Kalshi Create Order V2: https://docs.kalshi.com/api-reference/orders/create-order-v2
- Kalshi Demo environment: https://docs.kalshi.com/getting_started/demo_env
- Kalshi authenticated request/signing guide: https://docs.kalshi.com/getting_started/quick_start_authenticated_requests
- Kalshi API rate limits and tiers: https://docs.kalshi.com/getting_started/rate_limits
- Kalshi portfolio positions: https://docs.kalshi.com/api-reference/portfolio/get-positions
- Kalshi transaction fees (published April 19, 2026): https://help.kalshi.com/en/articles/13823805-fees
- OWASP Session Management Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
- scikit-learn TimeSeriesSplit (chronological validation): https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html
- Bailey et al., The Probability of Backtest Overfitting: https://papers.ssrn.com/sol3/Papers.cfm?abstract_id=2326253

## Limits of this validation

Offline unit, HTTP, fuzz and stress tests cannot prove a profitable trading edge, validate actual exchange execution, confirm a Render deployment, or replace a long-duration observation of real incoming signals. The current paper bot's strategy should remain unchanged while the research scanner is evaluated. Promote code only after a commit-specific CI run, service deployment, webhook-to-worker confirmation and rollback plan. Do not enable live orders as part of this readiness patch.
