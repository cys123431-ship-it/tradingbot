# Dynamic leverage policy

The five automatic futures strategies use one final, opportunity-aware
leverage decision after signal aggregation and risk scaling. Leverage is not a
fixed 5x value and is not a minimum-leverage search.

- 2-4x: stressed liquidity, elevated volatility, wide stops, or degraded
  strategy quality.
- 4-5x: ordinary accepted entries.
- 6x: exceptionally strong standalone entries with observable, supportive L2.
- 7-8x: at least two aligned strategies plus strong quality, normal volatility,
  a fresh signal, and a compact stop.
- 9-10x: at least three aligned strategies and the stricter high-conviction
  version of every quality check.

Leverage and stop-loss risk are deliberately separate. Raising leverage can
restore a risk-based position that was clipped by available margin, but it
cannot increase notional beyond the original stop-loss budget plan. Binance
isolated-margin and maintenance-bracket preflight remains authoritative and can
reduce the selected leverage before order submission.

Positions selected at 6-7x are monitored on 15-minute candles, receive an
approximately four-hour no-progress time stop, and arm trailing earlier.
Positions selected at 8-10x use the same 15-minute monitor with an approximately
two-hour no-progress time stop and the earliest trailing activation. The
original strategy exit profile is restored whenever a later reassessment no
longer qualifies for those tiers.

Design references:

- Man Group, *The Impact of Volatility Targeting*:
  https://www.man.com/insights/the-impact-of-volatility-targeting
- Man AHL, *Active Risk Management in Practice*:
  https://www.man.com/documents/download/bgege-06C4J-eyxtv-Fkkiw/Man_AHL_Insights_Active_Risk_Management_in_Practice_English_%28United_States%29_22-09-2021.pdf
- BIS CGFS Paper 34, *The role of valuation and leverage in procyclicality*:
  https://www.bis.org/publ/cgfs34.htm
- Binance USD-M Futures, *Change Initial Leverage* and *Notional and Leverage
  Brackets*:
  https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade#change-initial-leverage
  https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account#notional-and-leverage-brackets
