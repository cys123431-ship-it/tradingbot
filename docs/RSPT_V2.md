# RSPT-v2 and Dual agreement routing

## Purpose

RSPT-v2 turns Relative Strength Pullback Trend into an entry strategy that is
independent from UTBreakout. It is designed to contribute a different source of
signal to Dual instead of merely rechecking a direction already selected by UT.

This change does not guarantee profitability. Keep live risk small and compare
out-of-sample results against the legacy strategy before increasing allocation.

## RSPT-v2 signal flow

1. Use CoinSelector `selected` symbols as the only symbols eligible for entry.
2. Build a broader ranking universe from `selected + watch_only`, capped by
   `relative_strength_universe_size` (default: 30).
3. Fetch BTC and ETH reference candles and estimate each candidate's beta to
   those common market returns.
4. Rank candidates by volatility-normalized residual momentum:
   - long: top 20 percent by default;
   - short: bottom 10 percent by default.
5. Select direction from RSPT's own 1d and 4h trends.
6. Require a recent impulse, a controlled 0.4-1.2 ATR pullback, and a reclaim.
7. Reject stale or excessively chased entries.
8. Reduce position size when 4h ATR percentage is high or extreme.

The legacy forced-UT-direction behavior remains available when RSPT-v2 or
independent direction is explicitly disabled.

## Dual behavior

Dual evaluates UTBreakout and RSPT-v2 independently.

- Same direction: one selected entry plan is used at 100 percent of normal risk.
- Only one valid strategy: the plan is reduced to 60 percent of normal risk.
- Opposite directions: no trade; status code is
  `REJECTED_DUAL_DIRECTION_CONFLICT`.

The single-signal multiplier is configurable through
`dual_alpha_single_signal_risk_multiplier`.

## Risk and execution defaults

RSPT-v2 uses the pullback structure for the stop when available. The calculated
stop must be between 0.6 and 2.0 ATR from entry. Defaults also use:

- 25 percent partial take profit at 1.5R;
- ATR trailing activation at 2.0R;
- 2.75 ATR trailing distance;
- no immediate breakeven move after TP1;
- an 8-bar time stop when MFE remains below 0.5R;
- rejection when live price has moved more than 0.35 ATR adversely from the
  confirmed 4h close.

## Backtest month conversion

`--train-months` and `--test-months` now convert calendar months using the
selected candle interval. Examples for 30 days:

- 15m: 2,880 candles;
- 1h: 720 candles;
- 4h: 180 candles.

## Recommended validation

Compare the following under identical fees, slippage, funding and dates:

1. legacy UT Set64;
2. improved UT Set64;
3. legacy RSPT;
4. RSPT-v2;
5. legacy Dual;
6. Dual with independent agreement routing.

Track trade count, net expectancy, maximum drawdown, Sharpe/Sortino, profit
factor, long/short contribution and performance by market regime. Do not tune
on the final holdout period.
