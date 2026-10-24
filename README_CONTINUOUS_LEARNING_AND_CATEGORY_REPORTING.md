# Controlled paper learning and market-category reporting

## What this update does

- Uses the existing per-trade learning journal to record settled BTC trade context and outcomes.
- Keeps adaptation bounded: the existing learner may tighten the preferred BTC paper entry price only when the configured sample and loss criteria are met. It never changes the hard entry cap, trade size, daily loss budget, or live-trading permission.
- Adds a paper-only consecutive-loss circuit breaker. After the configured number of consecutive negative-net settled BTC trades, the worker pauses new entries for the cooldown. It still manages open positions before applying that gate. The default is four losses and 60 minutes.
- Ensures the fee-aware take-profit target cannot remain above Kalshi's 100-cent maximum contract bid. When the requested ROI is mathematically unreachable after fees, the target is capped at 100 cents and a warning is logged. That cap may yield a smaller realized ROI than requested.
- Adds independent research-paper reporting by category display group, including NFL, NBA, WNBA, NHL, MLB, NCAA Sports, Other Sports, Politics, Crypto Events, Economics, Weather, Financial Markets, Entertainment, Companies, World Events, and Science & Tech. Research trades remain in `market_research_paper_trades` and never enter the BTC `paper_trades` win-rate calculation.

## Important limitations

This is not an automatic code-rewriting AI. A loss is evidence to review, not proof that a prediction model is wrong. The bot records outcomes and makes only bounded, explicit policy changes. Broader strategy or model changes should be proposed only after chronological out-of-sample evaluation, with the old build retained for comparison and rollback.

The category-specific research pipeline will not invent probabilities or create entries if its HTTPS provider is absent, evidence is stale, the category is unsupported, model calibration fails, or risk checks fail. Sports league labels are for analytics only; every sports market still uses the base sports model and evidence gates.

Real-money execution remains locked. A signed low-level adapter is not equivalent to a fully verified order lifecycle. Use the Kalshi Demo environment first, then test order placement, partial fills, cancellations, retry/idempotency, reconcile after restart/timeout, fees, and emergency pause before reconsidering any live mode.

## Validation

Run `python -m pytest -q`, then `python validate_dashboard_js.py`, `python stress_phase456_readiness.py`, `python stress_market_decision_engine.py`, and `python smoke_shared_alert_flow.py`. These offline checks do not substitute for a Render staging test, real production journal audit, provider calibration, or a two-hour soak.
