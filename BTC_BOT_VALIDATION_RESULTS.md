# BTC Bot Master Upgrade — Validation Results

## Final status

- **27 isolated test modules passed: 117 test cases after the soft-target/hard-cap correction.** Each module was run as a separate process to avoid global/module stub state leaking between tests.
- **Offline end-to-end smoke test passed** from signed webhook intake through shared alert queue, hybrid decision, simulated paper entry, and dashboard heartbeat.
- `python -m compileall -q .`: passed.
- Both `.github/workflows/python-app.yml` and root `python-app.yml` parse as YAML, are byte-identical, and define the same 32-step workflow.
- Dashboard inline JavaScript passed `node --check` after extracting the latest script from `dashboard.html`.
- HTML structural validation found **204 element IDs with zero duplicate IDs**.
- Dashboard regression checks passed for the top-priority active-position panel, separated current-window WAIT state, preserved historical directional context, countdown/reasoning content, the learning panel, hard-cap/preferred-target labels, and chart-evidence score fallback when a WAIT row stores score 0.
- A local headless Chromium render using mocked API responses exercised both the no-position “Waiting For Next Available Signal” state and an open DOWN position while the current-window state remained WAIT; both rendered with “Dashboard connected” and no browser/console exceptions.
- Static safety scan found no assignment to dynamic `.innerHTML`; dashboard values are inserted through text-safe DOM APIs.
- Per-trade learning tests verify idempotency, unsettled-trade exclusion, legacy linkage through external alert ID, realized P&L classification, bounded adaptive-entry policy thresholds, one-way tightening, the 5¢ cap, 10-new-trade review watermark, and protection against historical backfill causing repeat adjustments.

## Test modules and case counts

| Test module | Cases | Result |
|---|---:|---|
| `test_paper_worker.py` | 9 | PASS |
| `test_trend_stop.py` | 4 | PASS |
| `test_hybrid_take_profit.py` | 17 | PASS |
| `test_paper_bot_fixes.py` | 4 | PASS |
| `test_signal_fusion_trend_gate.py` | 8 | PASS |
| `test_btc_trend_monitor.py` | 2 | PASS |
| `test_trend_monitor_worker.py` | 1 | PASS |
| `test_signal_engine.py` | 5 | PASS |
| `test_kalshi_api.py` | 3 | PASS |
| `test_orderbook_quote_pricing.py` | 3 | PASS |
| `test_entry_quality.py` | 7 | PASS |
| `test_current_signal.py` | 5 | PASS |
| `test_current_window_wait.py` | 4 | PASS |
| `test_wait_alerts.py` | 2 | PASS |
| `test_market_data_diagnostics.py` | 5 | PASS |
| `test_performance_audit.py` | 4 | PASS |
| `test_decision_audit.py` | 2 | PASS |
| `test_performance_analytics.py` | 2 | PASS |
| `test_trade_learning.py` | 8 | PASS |
| `test_analytics_api.py` | 1 | PASS |
| `test_counterfactual_audit.py` | 1 | PASS |
| `test_payload_safety.py` | 5 | PASS |
| `test_webhook_idempotency.py` | 1 | PASS |
| `test_category_registry.py` | 5 | PASS |
| `test_manual_api_security.py` | 4 | PASS |
| `test_app_http_surface.py` | 2 | PASS |
| `test_dashboard_context.py` | 3 | PASS |
| **Total** | **117** | **ALL PASS** |

The separate offline integration smoke test also passed:

```text
PASS: webhook received -> shared queue -> hybrid decision -> paper trade -> dashboard heartbeat
Decision: paper_opened UP KXBTC15M-26OCT222200-00 at 52.00¢ (SIMULATION_ONLY)
```

This uses deterministic local stubs and a temporary database. It does not submit real orders or prove a live Render integration.

## What the learning loop does

Each newly settled paper trade receives an idempotent entry-feature snapshot plus realized net-P&L classification. It stores observable context such as direction, chart bias, trend-score bucket, timeframe readings, entry price, momentum/volume alignment, chase warning, spread and linked entry rationale when available. The report compares cohorts and creates per-trade observational notes. Legacy trade rows without `entry_signal_log_id` can still recover their entry context from the associated external alert ID when that link exists.

**The learning loop now makes one bounded automatic adjustment in paper mode: it can tighten the preferred entry target by 1¢ after at least 20 settled hybrid outcomes, when at least 8 trades in the recent high-price target zone have net P&L at or below −8¢ and losses are at least as numerous as wins. It re-evaluates only after 10 additional newer hybrid trade IDs, never loosens, and stops at 5¢ below the initial target. It cannot change the 92¢ hard cap, trade budget, daily loss limit, or live-trading permission. All other strategy changes remain manual; one trade is never treated as causal proof.

## Limitations and deployment boundary

- Final dashboard layout puts Realized P&L, Engine connection, and Risk limits in the first overview row, followed by Active paper position. The current-window WAIT state, latest directional evaluation, active-position entry rationale, and historical context remain independent. The learning panel reports the effective target and adjustment reason. The last dashboard regression run also asserts 203 element IDs with no duplicates and JavaScript syntax validity.
- These tests are local and do not connect to production PostgreSQL or deploy to Render. PostgreSQL-specific migrations are covered by SQL-compatible design and adapter tests, but no live production database migration was exercised here.
- Direct headless Chromium navigation to the local server was blocked by the execution environment (`ERR_BLOCKED_BY_ADMINISTRATOR`), so browser UI checks used `page.set_content()` with mocked API responses instead. The mocked waiting and active-position states rendered without browser exceptions; this still does not validate the live Render service or its current production payloads.
- A passing test suite is not evidence that the strategy has positive expected value. The sample remains small and simulated.
- Non-BTC categories are readiness/research only; they are not connected to the BTC entry path.

## Required safety settings

- `TRADING_MODE=paper`
- `LIVE_TRADING_ENABLED=false`
- `PAPER_MAX_ENTRY_CENTS=92`
- `PAPER_ENTRY_TARGET_MAX_CENTS=80`
- `MAX_TRADE_DOLLARS=1.00`
- `MAX_DAILY_LOSS_DOLLARS=10.00`

No new environment variables are required for the trade-learning loop. `ensure_schema()` creates the journal table on startup; keep `DATABASE_URL` shared by the Web Service and Background Worker.

## Major-drop profit protection (paper-only)

After a paper position has reached and armed its fee-adjusted take-profit target, optional profit protection can trigger an immediate simulated exit when either (a) the internal engine detects a predefined major opposing setup, or (b) a newly closed 1-minute BTC candle moves adversely by at least `PAPER_TAKE_PROFIT_MAJOR_MOVE_ATR_MULTIPLIER` times the ATR baseline from prior closed candles. Configure `PAPER_TAKE_PROFIT_PROFIT_PROTECTION_ENABLED=true` and `PAPER_TAKE_PROFIT_MAJOR_MOVE_ATR_MULTIPLIER=2.5`. The exit is modeled from the observed Kalshi bid, subtracts entry and exit fees, and is logged as `PROFIT_PROTECTION`. This is best-effort only: gaps, wide spreads, missing liquidity, or polling delays can result in an exit below break-even; no no-loss outcome is guaranteed. It applies only to simulated paper positions and never enables real orders.

## Major-drop profit protection addition

After take-profit has previously armed on a fee-adjusted target, `PAPER_TAKE_PROFIT_PROFIT_PROTECTION_ENABLED=true` adds a conditional exit on a major opposing internal setup or an adverse, fully closed 1-minute BTC candle whose adverse move meets `PAPER_TAKE_PROFIT_MAJOR_MOVE_ATR_MULTIPLIER` times the ATR baseline from prior closed candles (default 2.5). It is not an exit on every trade. The simulated exit uses the actual observed Kalshi contract bid and subtracts entry and exit fees; it is recorded as `PROFIT_PROTECTION`. If the observed bid has already fallen below fee-adjusted break-even, the note explicitly records that a loss cannot be prevented at that quote. Polling delays, price gaps, slippage and missing liquidity mean no system can guarantee a no-loss exit. Tests cover large adverse movement on UP and DOWN positions, ordinary candles that must not trigger, and an integrated profit-armed protection exit.

### Entry-price semantics update

`PAPER_ENTRY_TARGET_MAX_CENTS` (default 80¢) is a soft preferred price, not a trade blocker. A directional signal that passes freshness, major-flaw, quote, spread, risk, and budget checks may enter above that preference up to `PAPER_MAX_ENTRY_CENTS` (default hard cap 92¢). Quotes above the hard cap wait and are not opened. Higher entry prices leave less maximum upside; a direction/evidence score is not a calibrated win probability.


## Entry target clarification and dashboard corrections

- `PAPER_ENTRY_TARGET_MAX_CENTS=80` is advisory only; it no longer blocks otherwise approved trades. `PAPER_MAX_ENTRY_CENTS=92` remains the actual hard cap. The code/test matrix permits a 90¢ ask if directional, market, quote, freshness and risk checks pass, and waits at 93¢.
- Dashboard risk card labels the 80¢ value as **Preferred entry target (soft)** and separately displays the configured **Hard entry cap**.
- Fixed the score mismatch: when a WAIT row uses score `0` as the action placeholder but has a saved chart-evidence score, the dashboard displays the chart-evidence score alongside the explanation rather than showing `0/100` against a reason like “58/100 below threshold.”
- For entries above the soft preference, the human-readable price warning is now persisted back to the linked signal payload consumed by the dashboard.
