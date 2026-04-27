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
