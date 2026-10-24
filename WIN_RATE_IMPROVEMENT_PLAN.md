# Win-rate and profitability improvement plan

## Baseline and uncertainty

The operator reports approximately 95 paper trades at a displayed win rate of 56%. If 95 trades are settled and 56% is rounded to the nearest whole percent, that is about 53 wins and 42 non-wins; read the actual settled journal to confirm the exact count. For 53/95, the Wilson 95% confidence interval is approximately 45.8%–65.4%. The current sample does not establish that the true win rate is above 50%, and open or break-even trades can change the interpretation.

A higher win rate alone is not the goal. A strategy can win frequently and still lose money if losses exceed wins or contracts are bought too close to $1. The primary objectives should be (1) positive fee/slippage-adjusted expectancy, (2) calibrated probability forecasts, (3) controlled drawdown, and (4) stability on unseen periods.

## Changes made in the current project

- Added model probability/evidence gates for multi-market research. Model output must be bound to the exact ticker, recent, out-of-sample validated, have at least 100 validation observations and Brier score no worse than 0.22, and provide required evidence types for its category.
- Added spread, depth, volume, price, expected-net-edge, fee/slippage and paper-risk gates. Invalid category, stale evidence, unknown pricing, thin books, or missing providers fail closed.
- Added a separate research-paper journal and settlement result analytics, so cross-market experiments do not alter the existing BTC strategy journal or its position logic.
- Added a 95% Wilson interval in performance analytics and the dashboard, plus net expectancy, profit factor and drawdown fields already used to avoid optimizing only for hit rate.
- Added property stress tests, a source-contract test suite, and mode/category/ticker safeguards.

## How to evaluate candidate improvements

1. Export a clean set of settled trades with entry price, direction, original signal, signal freshness, spread, time remaining, market regime, fees, result, and net P&L. Exclude open trades from the win-rate denominator.
2. Make a chronological split; fit/adjust on older trades and evaluate candidate changes on a later, untouched period. Never randomly mix future observations into training.
3. Compare baseline versus each candidate filter independently: minimum edge, minimum liquidity/depth, maximum spread, price range, time-to-close, signal/indicator agreement, weekday/weekend or trend regime. Keep a filter only if it improves out-of-sample net expectancy and does not worsen drawdown beyond the pre-agreed limit. Do not stack many filters on 95 trades.
4. Track calibration (Brier score and reliability bins) for actual forecast probabilities. An indicator score or market-implied probability is not a model probability.
5. Paper-test the candidate strategy in a separate journal. Do not automatically rewrite active strategy weights from the current short streak or enable live orders because a test looks favorable.

## Current limitation

The multi-market pipeline requires an operator-selected external HTTPS provider at `MARKET_ANALYSIS_PROVIDER_URL`. That provider must retrieve the actual current news/injury/lineup/official data and use a per-category model with independently audited out-of-sample calibration. The bot validates returned structure and timestamps but cannot truthfully certify source claims supplied by that provider. Until the provider exists and has been validated, it reports `provider_not_configured` and creates no research-paper entry.
