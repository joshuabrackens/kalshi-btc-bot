# BTC Bot Local Validation Results

## Final result after dashboard-context update
- **26 isolated test modules, 101 test cases passed.**
- `python -m compileall -q .`: passed.
- Both `.github/workflows/python-app.yml` and root `python-app.yml` parse as YAML, have the expected test job, match exactly, and contain 31 steps.
- Dashboard inline JavaScript passes `node --check`.
- HTML check: 175 element IDs, all unique.
- Dashboard state regression passed: a newer WAIT/price-wait evaluation does not erase the previous directional record or the original reasoning linked to an open paper position.
- Offline end-to-end smoke test passed.

## Offline end-to-end smoke
The smoke test printed:

```text
PASS: webhook received -> shared queue -> hybrid decision -> paper trade -> dashboard heartbeat
Decision: paper_opened UP KXBTC15M-26OCT222200-00 at 52.00¢ (SIMULATION_ONLY)
```

This is a deterministic test with stubbed public market feeds and a temporary local database; it does not submit real orders or prove live Render integration.

## Scope covered
Worker and stop tests; hybrid take-profit; score preservation; trend fusion; trend monitoring; signal-engine; Kalshi API retry behavior; order-book quote pricing; entry-quality gate; current signal/window behavior; WAIT alerts; market-data diagnostics; strategy performance audit; decision-audit schema; performance analytics; analytics API; rejected-signal counterfactual; payload safety; webhook idempotency/migration; category registry; manual API security; HTTP-surface integration; and dashboard signal/active-position context.

## Constraints and limitations
- Tests are local/isolated; Render services and production PostgreSQL were not accessed or deployed by this run.
- A passing test suite does not establish that a trading strategy has positive expected value.
- Non-BTC market categories are research-only; only BTC 15-minute paper flow is enabled.
- No live trading, order-placement implementation, or changed risk limits are enabled.
