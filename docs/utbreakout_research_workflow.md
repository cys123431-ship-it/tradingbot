# UT Breakout Research Workflow

This bot should not add more live filters just because an indicator looks good.
The workflow is:

1. Collect forward diagnostics from `/utbreakout research`.
2. Compare baseline UTBot variants with `scripts/utbreakout_backtest.py`.
3. Check TradingView visual parity with `tradingview/utbreakout_v1.pine`.
4. Promote only the tested pure strategy pieces into live code.

Research anchors:

- Lo, Mamaysky, Wang: technical analysis should be converted into objective algorithms.
- Brock, Lakonishok, LeBaron: moving average and trading-range breakout rules are useful baseline comparisons.
- Moskowitz, Ooi, Pedersen and long-horizon trend following literature: momentum/trend works only when costs, volatility, and drawdown are controlled.
- Bailey and Lopez de Prado: many-set selection must be treated as an overfitting problem.

Operational checks:

- If one set is selected in more than 50% of recent AUTO events, review score concentration before changing live settings.
- If rejection is dominated by one reason, inspect that filter before assuming the market is simply bad.
- If average planned margin or slippage drifts up, treat it as execution/risk pressure rather than a strategy edge.

## Generalization controls

- Run walk-forward reports with a non-zero purge gap. The default CLI gap is four candles and can be changed with `--wf-purge-candles`.
- Walk-forward selection uses the worst score across the full training window and its chronological first/second halves. This favors time stability over a single full-sample peak; `--no-wf-robust-selection` exists only for baseline comparisons.
- Review `selection_concentration`, `mean_expectancy_retention`, and `mean_generalization_gap_r`; do not promote a variant only because its full-sample score is highest.
- Keep the final holdout period untouched until the strategy and thresholds are frozen.
- Live entry uses the integrated Entry Edge decision once; downstream quality checks do not reapply the same score or probability thresholds.
- Recent realized expectancy is diagnostic and position-sizing input by default. It can reduce risk, but it does not raise the alpha score, increase risk after a lucky streak, or block an otherwise valid entry.
- Strict expectancy, profit-factor, multiple-testing, and purged OOS requirements remain in research reports. Live hard blocking is an explicit opt-in, not the default.
