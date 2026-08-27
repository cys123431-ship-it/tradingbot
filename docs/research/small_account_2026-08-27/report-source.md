# Sub-$1,000 strategy evidence ledger (2026-08-27)

## Question and scope

Determine whether the live sub-$1,000 strategy should be replaced or improved,
using finalized Azure trade records, the current implementation, and primary or
peer-reviewed research. This document is an evidence ledger for the code change;
it is not a claim that future profit is guaranteed.

## Live evidence used

- The last 60 finalized records contained 33 bot-managed exits and 27
  manual/external or emergency exits.
- Bot-managed exits were profitable as a group, while manual/external and
  emergency exits were negative as a group. Therefore the current champion
  trend engine was retained and non-strategy exits were excluded from adaptive
  strategy allocation.
- Recent plan metadata had only a small number of observations under the current
  convex score version. No new hard score threshold was fitted to that sample.
- The reversal challenger still has fewer than the required independent shadow
  outcomes and remains shadow-only until the existing promotion tests pass.

## Claim-source ledger

| Claim used in the design | Source | Scope/limitation | Code consequence |
|---|---|---|---|
| Trend following can work in liquid crypto futures, and volatility scaling is central. A compact liquid universe can be preferable once trading costs are considered. | Man AHL, *In Crypto We Trend* (2024), https://www.man.com/insights/in-crypto-we-trend | Institutional simulation; not this bot's live account. | Retain the volatility-scaled trend champion and the existing liquidity/cost gates. Do not replace it with an unvalidated complex model. |
| Trend speeds have complementary behavior; faster signals can react to reversals but incur higher turnover and execution costs. | Man AHL, *The Need for Speed in Trend-Following* (2023), https://www.man.com/insights/need-for-speed-trend-following | Broad futures evidence, not crypto-only. | Blend fast, medium and slow scores by weight inside each timeframe. Do not require all speeds to agree. Keep explicit cost subtraction. |
| Crypto momentum is concentrated in persistent up-to-up states and is weak in transitional/down states. | *State transitions and momentum effect in cryptocurrency market* (2025), https://doi.org/10.1016/j.frl.2025.108356 | Cross-sectional portfolio study; the bot trades one position at a time. | Add a bounded persistence bonus and a transition penalty to the trend router rather than a new hard entry veto. |
| Futures basis contains cross-sectional crypto return information; short-horizon results are stronger than monthly results and the long leg is important. | Chi, Hao & Lau, *Crypto carry* / Journal of Futures Markets, https://onlinelibrary.wiley.com/doi/full/10.1002/fut.22425 | The paper's basis sign differs from this bot's `(mark-index)/index` convention. | Apply a small direction-aware basis adjustment with the sign explicitly translated; retain existing crowding safeguards. |
| Order flow contains persistent information about subsequent crypto returns. | *Order flow and cryptocurrency returns*, Journal of Financial Markets (2026), https://doi.org/10.1016/j.finmar.2026.101047 | Exchange/microstructure results do not imply every snapshot is tradable. | Use only fresh (at most 90 seconds) signed imbalance/taker evidence and cap its probability adjustment. Stale data gets no boost. |
| Crypto cross-sectional returns are related to market, size and momentum; simple predictors such as momentum and illiquidity can be competitive with complex ML. | Liu, Tsyvinski & Wu, *Common Risk Factors in Cryptocurrency*, Journal of Finance, https://doi.org/10.1111/jofi.13119; *Machine learning and the cross-section of cryptocurrency returns*, IRFA (2024), https://doi.org/10.1016/j.irfa.2024.103244 | Portfolio studies; extreme small-coin returns can be difficult to execute. | Keep the existing relative-strength/liquidity selector and avoid introducing a high-dimensional ML/RL strategy from a small live sample. |

## Design decision

The evidence does not support replacing the current live champion. The safer
high-upside change is an evidence router v3:

1. Blend EMA 8/21, 20/50 and 50/100 trend speeds within each of 15m, 1h, 4h and
   1d, then retain the existing weighted multi-timeframe decision.
2. Compare the current state with a lagged completed-bar state and add a bounded
   probability bonus for the research-supported persistent up-to-up long state.
   Other trend transitions receive a small bounded penalty. This is a soft score,
   not an AND gate or a ban on short entries.
3. Add small, capped basis and fresh order-flow adjustments. Existing downstream
   execution, spread, depth, funding and crowding gates remain authoritative.
4. Label the router probability as a bounded heuristic, not a calibrated
   forecast, and store its components in durable trade metadata.
5. Exclude user/manual, external-exchange and EmergencyStop outcomes from the
   adaptive strategy allocator. They remain in accounting/reporting.
6. Keep the exhaustion reversal challenger shadow-only until the existing 100
   trade, regime-diversification, PBO, deflated-Sharpe and walk-forward gates pass.

## Rejected alternatives

- No leverage increase: the request concerns entry quality, and leverage is
  already handled by a separate volatility/liquidation-aware module.
- No fitted hard threshold from the latest few trades: the sample is too small
  and would recreate the overfitting problem.
- No black-box ML/RL policy: available live labels are sparse and contaminated
  by user exits unless carefully curated.
- No immediate live reversal promotion: its independent shadow sample is not
  yet sufficient.

## Verification contract

- Pure router unit tests must prove weighted (not AND) multi-speed behavior,
  persistence preference, stale order-flow neutrality, and basis sign handling.
- Allocator tests must prove manual/external and emergency outcomes are excluded.
- Full repository tests, deployment CI and read-only Azure state checks are
  required before the change is considered complete.
