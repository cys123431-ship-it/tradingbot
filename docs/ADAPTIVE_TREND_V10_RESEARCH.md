# Adaptive Trend v10 Research Overlay

Status: **research branch only**. Do not promote to live solely from this document.

## Motivation

The parent `adaptive_breakout_trend_v1` already has multi-horizon volatility-normalized
momentum, EMA structure, breakout/continuation entry shapes, L2 safety, change-point
flow, and winner-only pyramiding. The research overlay therefore does **not** add a
new prediction stack. It post-processes an already-approved candidate using three
risk/economic dimensions:

1. **Regime persistence** — mature momentum is discounted when the fast sleeve
   turns against the slower trend.
2. **Volatility-managed exposure** — size is reduced materially when realized
   volatility rises instead of being effectively pinned near full size.
3. **Perpetual carry/crowding** — direction-signed funding and basis reduce score
   and size continuously, with an extreme-crowding veto for stretched mature
   continuations.

The overlay can reject or reduce an entry. It can **never increase** position size.
It does not modify stop-loss prices/distances, take-profit geometry, runner policy,
or Adaptive Trend pyramiding triggers.

## Research basis

- Moreira & Muir, *Volatility-Managed Portfolios*, Journal of Finance (2017);
  NBER Working Paper 22208. Less risk in high-volatility states can improve
  risk-adjusted performance when expected return does not rise proportionally
  with volatility. https://www.nber.org/papers/w22208
- Barroso & Santa-Clara, *Momentum Has Its Moments*, Journal of Financial
  Economics 116(1), 2015. Time-varying momentum risk is predictable and volatility
  scaling substantially improves momentum risk-adjusted performance.
  https://doi.org/10.1016/j.jfineco.2014.11.010
- Yang, *Cryptocurrency market risk-managed momentum strategies*, Finance Research
  Letters 85A (2025), 107879. Risk-managed crypto momentum improves Sharpe in the
  paper's weekly cross-sectional setting. https://doi.org/10.1016/j.frl.2025.107879
- *State transitions and momentum effect in cryptocurrency market*, Finance
  Research Letters 86A (2025), 108356. Crypto momentum profitability is strongly
  state-dependent, motivating an explicit persistence/transition filter.
  https://doi.org/10.1016/j.frl.2025.108356
- Cao, Luo, Cheng & Dong, *Anatomy of Cryptocurrency Perpetual Futures Returns*,
  SSRN working paper (2026). Basis and a price-volume factor explain a broad set
  of perpetual-futures return predictors, motivating continuous funding/basis
  treatment. https://doi.org/10.2139/ssrn.6795783
- Gornall, Rinaldi & Xiao, *Perpetual Futures and Basis Risk: Evidence from
  Cryptocurrency*, SSRN (2025). Funding, constrained arbitrage and speculative
  demand are central to perpetual-futures basis behavior.
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5036933

## Important limitations

The cited studies do not directly prove profitability for this bot, its 1-hour
timeframe, its Binance universe, or its execution costs. Some evidence is
cross-sectional and some 2026 material is still a working paper. The thresholds
in this branch are therefore conservative hypotheses, not optimized parameters.

The Azure runtime log is `/home/azureuser/emas.log`, but this research branch was
created before a direct server-log session was available. Before live promotion,
inspect real accepted/rejected entries, realized slippage/funding, MFE/MAE and
winner-pyramid outcomes, then run walk-forward/out-of-sample tests with fees and
funding.

## Promotion gate

Do not merge to `main` until all of the following are satisfied:

- server logs have been inspected for the recent live period;
- old-vs-v10 decisions are replayed on the same candles;
- fees, slippage and funding are included;
- walk-forward/OOS results do not rely on one symbol or one market regime;
- max drawdown / tail loss does not materially worsen;
- no stop/TP/pyramiding geometry is changed by the overlay;
- live deployment starts with a limited observation/low-risk phase.
