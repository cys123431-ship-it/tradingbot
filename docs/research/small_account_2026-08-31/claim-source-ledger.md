# Claim-source ledger

| Claim | Primary evidence | Implementation boundary |
|---|---|---|
| Reducing exposure during volatility spikes can improve risk-adjusted outcomes. | Moreira & Muir (NBER 22208); Daniel & Moskowitz (NBER 20439). | Volatility ratio only reduces the scale; it never raises risk or predicts direction. |
| Multiple trend speeds contain complementary information. | Hurst, Ooi & Pedersen; Man AHL. | Existing weighted 13-timeframe router remains; no all-timeframes AND gate is added. |
| Order-flow imbalance is informative but depth-dependent and short-lived. | Cont, Kukanov & Stoikov. | Existing L2/liquidity gates remain authoritative; order flow is not counted again in sizing. |
| More searched rules increase overfitting risk. | Bailey et al. | Reuse existing metrics, use a bounded monotonic scale and add no fitted entry threshold. |
| A smaller position at the same stop price lowers money loss. | Position-risk identity: quantity x entry-to-stop distance. | Scale quantity/budget once; preserve `stop_loss` and ROE trailing configuration. |
