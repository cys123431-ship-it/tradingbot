# Sub-$1,000 risk-managed momentum allocation (2026-08-28)

## Research question and scope

Improve the live sub-$1,000 futures strategy without merely adding more entry
filters, fitting thresholds to a handful of recent trades, or increasing risk
twice through overlapping sizing modules. The review combines finalized Azure
trade records, the current execution path and research on crypto momentum,
market states, multiple trend speeds, volatility management, order flow,
transaction costs and stop-loss behavior.

This is an evidence and implementation ledger, not a profitability guarantee.
Portfolio-level weekly studies do not directly validate a one-position intraday
futures bot, so only bounded changes with an explicit fallback were adopted.

## Method

1. Reconstructed all 132 finalized live records and separated likely
   bot-managed outcomes from manual, external-exchange and emergency outcomes.
2. Audited the route from candidate selection through the final
   `apply_dynamic_leverage_to_plan` call used immediately before an order.
3. Compared supporting and disconfirming research, prioritizing peer-reviewed
   and primary sources.
4. Rejected parameters that would be fitted to the two finalized v3 trades.
5. Added a pure, observable allocation decision and proved its final sizing
   behavior with unit, regression and full-suite tests.

## Live evidence and limitations

- The database contained 132 finalized records. A conservative reason/leg-based
  classification identified 52 likely bot-managed and 80 manual/external or
  emergency outcomes. The bot-managed subset was negative overall, but that
  classification is imperfect because older records lack newer provenance
  fields.
- The v3 evidence router had only two finalized outcomes: AAVE long
  (-0.593R) and NVDA long (-0.277R). The current WLD short was still open during
  development. This sample is far too small for a new fitted entry threshold.
- The audit found that v3 evidence changed router metadata while the final
  small-account sizing still used the same 65% first-stage margin for base,
  strong and elite evidence. Leverage tiers changed, but the research evidence
  did not independently change how much of the 95% margin budget entered at the
  first stage.
- The trend/event OR resolver also dropped the trend engine's
  `fresh_continuation` field. As a result, the downstream mature-trend check and
  any allocation rule that required a fresh continuation could not observe the
  original decision. The resolver now preserves that field for trend-derived
  outcomes.
- Existing stop distance, liquidation buffer, per-position maximum loss,
  exchange margin, spread/depth and protection-order checks remain the final
  authorities.

## Claim-source ledger

| Claim | Evidence | Scope and limitation | Implementation consequence |
|---|---|---|---|
| Risk-managing crypto momentum can raise returns as well as Sharpe; the reported average scaling factor was about 1.14. | *Cryptocurrency market risk-managed momentum strategies*, Finance Research Letters (2025), https://doi.org/10.1016/j.frl.2025.107879 | Weekly cross-sectional portfolios, not leveraged intraday orders. | Use 1.15 as the first bounded strong-evidence margin multiplier. Do not add a second inverse-volatility multiplier because final sizing already uses ATR/stop/liquidation limits. |
| Crypto momentum is concentrated in persistent UP-UP states and largely absent in other state transitions. | *State transitions and momentum effect in cryptocurrency market* (2025), https://doi.org/10.1016/j.frl.2025.108356 | Weekly market states; strongest conclusion is asymmetric and does not support a symmetric short boost. | Extra allocation is long-only and requires the existing multi-timeframe proxy to be `persistent_up`; shorts retain existing allocation rather than being blocked or reduced. |
| Price trend predictors are more useful at daily/weekly horizons, while volume evidence varies by horizon. | *Trend-based forecast of cryptocurrency returns*, Economic Modelling (2023), https://doi.org/10.1016/j.econmod.2023.106323 | Forecasting study, not a direct execution rule. | Require complementary multi-speed and multi-timeframe price evidence. Volume alone cannot unlock more risk. |
| Different trend speeds are complementary, but fast signals cost more to trade. | Man AHL, *The Need for Speed in Trend-Following*, https://www.man.com/insights/need-for-speed-trend-following | Institutional futures evidence, not crypto-only. | Keep the existing weighted speed ensemble, not an all-speeds AND gate; require at least 0.65 agreement for extra allocation. |
| Cross-sectional crypto momentum and simple relative-strength predictors can be economically meaningful. | Liu, Tsyvinski & Wu, *Common Risk Factors in Cryptocurrency*, Journal of Finance, https://doi.org/10.1111/jofi.13119; *Machine learning and the cross-section of cryptocurrency returns*, IRFA (2024), https://doi.org/10.1016/j.irfa.2024.103244 | Portfolio studies; small and illiquid coins can be hard to execute. | Require a valid ranked universe of at least five symbols and top-quartile relative strength before expansion; existing liquidity gates stay mandatory. |
| Signed order flow contains information about subsequent crypto returns, but a stale snapshot should not be treated as current conviction. | *Order flow and cryptocurrency returns*, Journal of Financial Markets (2026), https://doi.org/10.1016/j.finmar.2026.101047 | Microstructure relation does not imply a deterministic trade. | Require positive direction-aligned order flow no older than 90 seconds for expansion. It remains a corroborator, not an independent entry. |
| Intraday crypto exhibits both momentum and reversal depending on jumps, liquidity and events. | *Intraday momentum and reversal in cryptocurrency markets*, North American Journal of Economics and Finance (2022), https://doi.org/10.1016/j.najef.2022.101733 | Concentrated in BTC and larger coins; not universal across all altcoins. | Event-only and reversal candidates receive no risk-managed momentum boost; their existing specialized allocation remains unchanged. |
| Popular crypto predictors often fail out of sample, and complex models do not consistently beat simple economic models. | *Out-of-sample forecasting of cryptocurrency returns: A comprehensive comparison of predictors and models*, Physica A (2022), https://doi.org/10.1016/j.physa.2022.127379 | Forecasting target differs from this bot's trade label. | No ML/RL layer and no hard threshold fitted to recent losses. Store the decision inputs for future independent validation. |
| Stop-loss rules improved crypto momentum payoffs and downside behavior across multiple thresholds in the study sample. | *Stop-loss rules and momentum payoffs in cryptocurrencies*, Journal of Behavioral and Experimental Finance (2023), https://doi.org/10.1016/j.jbef.2023.100833 | Monthly portfolio stop rules are much wider than this bot's ATR stops. | Preserve the existing hard SL and staged ROE protection. This change does not loosen or replace protection orders. |
| Inverse-volatility scaling can improve risk-adjusted factor returns when expected returns do not rise proportionally with volatility. | Moreira & Muir, *Volatility-Managed Portfolios*, Journal of Finance / NBER, https://www.nber.org/papers/w22208 | Broad factors, not crypto-specific. | Do not multiply exposure again: the existing final leverage selector already uses stop distance, volatility, liquidation distance and loss caps. |

## Evidence gap matrix

| Decision | Supporting evidence | Disconfirming / missing evidence | Resolution |
|---|---|---|---|
| Replace the current trend/event engines | Recent bot-managed loss and v3 losses show improvement is needed. | Only two finalized v3 trades; research remains mostly portfolio-level. | Do not replace the engines or fit a new veto. Improve allocation observability and evidence linkage. |
| Increase every qualifying trade | User objective permits risk for higher return. | Intraday momentum is regime-dependent; unconditional scaling magnifies false breaks. | No blanket increase. Ordinary entries keep exactly 65%. |
| Expand persistent strong crypto longs | Risk-managed momentum, UP-UP asymmetry, relative strength, multi-speed and flow evidence align. | Exact 1.25 elite multiplier is a bounded engineering choice, not a paper estimate. | Strong uses 1.15; elite uses 1.25; both remain capped at 90% of the 95% budget and downstream loss limits. |
| Expand shorts symmetrically | Trend following can profit in down markets. | The state-transition result specifically finds crypto momentum concentrated in UP-UP, not symmetric persistent down states. | Shorts are neither blocked nor reduced, but receive no new expansion. |
| Add another volatility multiplier | Volatility-managed portfolios have strong evidence. | Existing sizing already applies stop/ATR/liquidation-aware leverage; a second multiplier would double count. | Keep one final sizing authority. |
| Increase event-only or reversal entries | Diversification may help when trend fails. | Intraday momentum/reversal varies with event and liquidity state; reversal challenger lacks promotion sample. | Preserve their current base/capped allocation; no boost. |

## Adopted behavior

The new v4 risk-managed momentum allocator changes only future crypto
small-account entries:

- Ordinary, short, event-only, TradFi, stale-flow or transitional signals keep
  the existing first-stage fraction of 65%.
- Strong expansion requires: a trend/aligned long, fresh continuation,
  persistent-up multi-timeframe state, speed agreement at least 0.65,
  persistence at least 0.60, risk tier strong/elite, score at least 70,
  percentile at least 75, a ranked universe of at least five, fresh supportive
  order flow and no materially adverse basis adjustment.
- Strong entries use `0.65 × 1.15 = 0.7475` of the margin budget.
- Elite entries additionally require elite tier, score at least 82, percentile
  at least 90, speed agreement at least 0.80 and persistence at least 0.75;
  they use `0.65 × 1.25 = 0.8125`.
- The fraction is capped at 0.90. It is consumed once by the existing final
  dynamic sizing path. It does not multiply the risk percentage or leverage a
  second time.
- All evidence, the selected multiplier and the reason survive durable plan
  persistence for restart/recovery and later analysis.

## Rejected alternatives

- Raising all entries to a larger fixed margin: no regime discrimination.
- Tightening entry thresholds after two v3 losses: direct overfitting.
- Reducing ordinary entries: contradicts the requested high-return profile and
  would repeat the prior over-conservatism problem.
- Symmetric short expansion: not supported by the state-transition evidence.
- Immediate promotion of the reversal challenger: its independent sample gate
  is not met.
- A new ML/RL predictor: labels are sparse, strategy versions changed over time
  and older exit provenance is incomplete.

## Verification

- Focused strategy, dynamic sizing and adaptive trend tests: 97 passed.
- Full repository suite: 1,191 passed, 5 skipped.
- The tests prove elite 81.25% margin reaches the final order sizing exactly
  once, while short, event-only, TradFi and stale-flow cases remain at 65%.
- The open WLD position was not mutated during development; its existing SL and
  TP remained visible in the read-only monitor snapshot.
