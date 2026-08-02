# Five-strategy research revision 2

This revision improves every live branch without fitting thresholds to the
small and mixed live/testnet trade sample.  The implementation uses broad,
scale-free features (volatility ratios, normalized returns, OI changes and
completed-candle structure) and keeps the shared liquidity, protection and
single-position controls intact.

## Research basis

- Hurst, Ooi and Pedersen, *A Century of Evidence on Trend-Following
  Investing*: multi-market time-series momentum is persistent across a long
  sample.  This supports keeping diversified trend horizons rather than
  optimizing one lookback.
  https://www.aqr.com/insights/research/journal-article/a-century-of-evidence-on-trend-following-investing
- Moreira and Muir, *Volatility-Managed Portfolios*: recent variance is
  forecastable and inverse-volatility exposure can improve risk-adjusted
  returns.  This motivates continuous volatility targeting instead of fixed
  high-volatility buckets.
  https://www.nber.org/papers/w22208
- Daniel and Moskowitz, *Momentum Crashes*: momentum losses cluster after
  strong runs and volatility/turbulence changes.  This motivates the VMT
  adverse-return shock and directional-energy checks.
  https://www.nber.org/papers/w20439
- Liu and Tsyvinski, *Risks and Returns of Cryptocurrency*: cryptocurrency
  returns exhibit time-series momentum distinct from traditional macro risk
  factors.  This supports residual and multi-horizon crypto momentum while
  avoiding a single benchmark signal.
  https://www.nber.org/papers/w24877
- Man AHL, *Trend-Following: Rolling With the Punches*: institutional trend
  implementations cut exposure after volatility spikes and can re-establish
  it after conditions stabilize.  This supports fast continuous de-risking,
  not permanent low exposure.
  https://www.man.com/documents/download/f483b-092e2-337ea-54db2/Man_AHL_Insights_Trend-following%3A_Rolling_With_the_Punches_English_%28United_States%29_04-04-2023.pdf
- Bank for International Settlements, *Crypto carry*: crypto futures leverage,
  basis/carry and forced-liquidation risk are linked, so funding extremes are
  not treated as a risk-free reversal signal.  OI must begin unwinding before
  the crowding branch can enter.
  https://www.bis.org/publ/work1087.pdf

These papers support a design direction, not a promise of profitability.
Thresholds remain deliberately broad and require forward validation.

## Implemented changes

### UTBreak

- Replaced the abrupt 40% high-volatility haircut with continuous inverse-ATR
  targeting between the normal and extreme-volatility boundaries.
- Keeps more exposure just above normal volatility and progressively reduces
  it as volatility approaches the existing hard shock boundary.

### RSPT-v3

- Normalizes fast and slow residual momentum separately so the shorter horizon
  is not mechanically diluted by the long window.
- Shrinks conflicting fast/slow residual momentum and scales risk continuously
  by rank conviction; a marginal top-quintile candidate is smaller than the
  strongest candidate.
- Replaced fixed volatility buckets with continuous inverse-ATR targeting.

### Volatility Managed Trend

- Adds direction-aware favorable/adverse return energy and rejects a fresh
  adverse shock that contradicts the still-lagging EMA trend.
- Uses a three-bar median volume confirmation rather than one potentially
  anomalous candle.
- Separates signal quality budget from volatility scaling, allowing risk to
  fall below the former fixed floor in an actual volatility shock while strong,
  stable trends can still use the configured cap.

### Crowding Unwind

- Requires completed price-structure reversal and an actual 1h OI decline.
  Funding, OI build and long/short crowding alone can no longer trigger a
  contrarian entry.
- Treats L2 direction as execution information rather than independent alpha;
  stressed liquidity still blocks through the shared gate.
- Handles missing funding percentile or OI z-score safely when the alternative
  absolute funding/OI-change evidence is available.

### Liquidation Exhaustion Reversal

- Requires the reversal candle to close near its favorable extreme.
- When dynamic L2 history is available, requires at least two observations and
  rejects widening spread or continued support-side depth depletion.
- Scales risk by both setup quality and liquidity recovery instead of preserving
  a fixed minimum through stressed conditions.
