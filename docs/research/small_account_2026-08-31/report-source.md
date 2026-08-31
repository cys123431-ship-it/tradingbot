# Sub-$1,000 stability-risk refinement (2026-08-31)

## Decision

Keep the existing entry engines, 13-timeframe weighted router, 4-7x leverage,
65% first stage, 8/12/16% configured campaign ceilings and operator-selected
ROE trailing staircase. For future entries only, scale the final campaign
quantity from 1.00 down to 0.80 when accepted multi-timeframe evidence is
transitional/mixed or short-horizon volatility has jumped relative to its
longer baseline.

The scale is applied once to both available campaign margin and its maximum
loss budget. It cannot increase exposure, cannot block an accepted entry and
does not alter the ATR/structure stop price. Existing positions retain their
persisted entry and exit plan.

## Why this design

- Volatility-managed portfolios reduce risk when volatility rises because
  expected returns generally do not rise proportionally with variance.
- Momentum losses cluster in high-volatility panic/rebound states, supporting
  bounded exposure reduction rather than a new direction predictor.
- Multiple trend speeds are complementary. A weighted ensemble avoids the
  brittleness and low participation of an all-timeframes AND rule.
- Short-horizon order-flow evidence is useful for execution context, but is not
  reused here because the live path already applies liquidity/L2 safety gates.
- Trying many additional indicator thresholds increases backtest-overfitting
  risk. The change therefore uses only signals already produced by the live
  strategy and adds no new entry veto.

## Scope and limitations

This is a risk-control refinement, not evidence that the strategy will be
profitable. The mapping is deliberately bounded and monotonic. No recent-trade
threshold fitting or claimed backtest performance is used.

## Sources

- Moreira & Muir, *Volatility Managed Portfolios*, Journal of Finance / NBER:
  https://www.nber.org/papers/w22208
- Daniel & Moskowitz, *Momentum Crashes*, Journal of Financial Economics / NBER:
  https://www.nber.org/papers/w20439
- Hurst, Ooi & Pedersen, *A Century of Evidence on Trend-Following Investing*:
  https://www.aqr.com/Insights/Research/Journal-Article/A-Century-of-Evidence-on-Trend-Following-Investing
- Man AHL, *The Need for Speed in Trend-Following*:
  https://www.man.com/insights/need-for-speed-trend-following
- Cont, Kukanov & Stoikov, *The Price Impact of Order Book Events*:
  https://arxiv.org/abs/1011.6402
- Bailey et al., *The Effects of Backtest Overfitting on Out-of-Sample Performance*:
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2308659
