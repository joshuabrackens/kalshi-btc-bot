# BTC Bot Scanner and Autopilot Fix — Validation Report

**Prepared:** 2026-10-24 (UTC)  
**Scope:** standard + multivariate Kalshi catalog pagination, automatic research-to-paper path, credential/provider gate, current official orderbook schema, dashboard controls, regression coverage.

## Changes in this release

- The catalog scanner consumes both public `GET /events` and `GET /events/multivariate` with nested markets and uses separate opaque-cursor prefixes for the two streams.
- Failure states now keep safe HTTP status, failed page number, retryability code, and resume/restart information, without displaying opaque cursor values or response bodies. Transient 408/425/429/5xx/network failures receive bounded retry handling. A rejected continuation cursor gets one clean restart.
- `diagnose_kalshi_catalog.py --pages 5` is now a read-only Render Shell diagnostic that checks several pages from each catalog feed and reports per-page response shape/status without displaying cursor values or response bodies.
- The opportunity engine parses current Kalshi `orderbook_fp.yes_dollars/no_dollars` price/quantity arrays as well as the legacy `orderbook.yes/no` cent format. Unknown book shapes still fail closed instead of inventing depth.
- Provider readiness requires both a valid HTTPS provider URL and a non-empty shared bearer token. This matches the provider service authentication requirement and prevents the dashboard from enabling automation when the URL exists but the provider rejects every request.
- GitHub Actions checks for market catalog diagnostics and provider-service security tests were added to the mirrored workflow files.
- The master dashboard autopilot toggle can start/resume the scan and research scheduler, but the resulting automatic entries are isolated paper research trades only. Live orders are not submitted by the catalog or research pipeline.

## Test results in this environment

- `python -m compileall -q .`: PASS
- `pytest -q`: **233 passed** (latest full suite)
- `python stress_market_decision_engine.py`: PASS, 25,000 randomized decision cases, no uncaught exceptions, no exchange orders
- `python stress_phase456_readiness.py`: PASS, including 100,000 metadata cases, 20,000 mode-gate checks, 10,000 session-token checks; inventory cap and live-off invariants held
- `python smoke_shared_alert_flow.py`: PASS, deterministic offline webhook-to-paper smoke flow
- `python validate_dashboard_js.py`: PASS
- YAML parse for `.github/workflows/python-app.yml`: PASS; root `python-app.yml` is byte-identical
- Current official orderbook fixed-point schema regression tests: PASS

The stress tests use deterministic/fuzzed fixtures; they do not submit orders or contact the live order API.

## What is not yet verified

- The updated source has not yet been deployed to the user's Render service in this session. The existing production error may still be caused by upstream status/rate-limit/cursor behavior, which will be visible after this patch is deployed.
- A continuous 1-hour or 2-hour soak is **not complete**. The local execution environment interrupted a previous long run. The included `run_validation_soak.py` requires at least 7,200 seconds of accumulated test-command runtime; the report must say `status=passed` and `completed_test_command_seconds >= 7200` to count as complete.
- The user's real trade journal was not supplied as an export, so historical outcomes have not been replayed in this validation.
- The analysis provider must be deployed/configured and return accurate, source-attributed outputs. The paper research pipeline requires realized, out-of-sample calibration per exact category/model/version (at least 100 resolved predictions, both YES and NO represented, Brier <= 0.22) before creating eligible research paper entries. When the provider or calibration is missing, no entry should be created.
- Live order execution remains locked and is not wired to the research pipeline. Keep `TRADING_MODE=paper`, `LIVE_TRADING_ENABLED=false`, `LIVE_TRADING_APPROVED=false`, and `MARKET_LIVE_EXECUTION_ENABLED=false`.

## Recommended deployment check

After committing this patch to a separate upgrade branch and passing GitHub Actions, deploy the same commit to the Web Service and Background Worker. Then run this read-only command in Render Shell:

```bash
python diagnose_kalshi_catalog.py --pages 5
```

If it fails, inspect the per-feed/page HTTP status. Do not paste API keys, private key file contents, opaque cursors, or full response bodies into chat. The dashboard scanner status should then report an error code and failed page number rather than only a generic failure.

## Upstream schema reference

- [Kalshi Get Market Orderbook](https://docs.kalshi.com/api-reference/market/get-market-orderbook) documents `orderbook_fp` with `yes_dollars` and `no_dollars` price-level arrays.
- [Kalshi Get Multivariate Events](https://docs.kalshi.com/api-reference/events/get-multivariate-events) documents a separate paginated `/events/multivariate` feed with nested markets.
