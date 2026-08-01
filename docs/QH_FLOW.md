# VMT, Shared L2 Gate and Triple

## QH retirement

`qh_flow_v1` no longer originates live entries and no longer confirms, reduces,
or blocks UTBreak/RSPT entries. Persisted QH selections and old Telegram `qh`
callbacks are migrated to `volatility_managed_trend_v1`. The QH module remains
only because its tested order-book helpers implement the shared L2 safety gate.

## Volatility-Managed Trend (VMT)

`volatility_managed_trend_v1` evaluates completed Binance USDT perpetual 1h
candles. It combines 8h, 24h and 72h time-series momentum after normalizing each
horizon by realized volatility.

The live decision requires:

- at least two same-direction horizons and no opposite slow-horizon vote;
- aligned 12/36 EMA trend and slope;
- sufficient path efficiency and volume participation;
- no short-term volatility shock or excessive EMA extension;
- a non-stressed shared L2 order book;
- a fresh live price that has not chased more than 0.5 ATR beyond the signal.

Risk is volatility scaled and capped at 60 percent for a standalone VMT signal.
Accepted entries use the existing risk plan, margin cap, single-position guard,
order gateway, TP/SL protection, reconciliation and Telegram reporting paths.

## Shared L2 Gate

The shared L2 gate evaluates the top 20 bid and ask levels.

- `CALM`: normal risk.
- `MIXED`: reduced risk, default 65 percent.
- `STRESSED`: new entries blocked.

The gate is applied independently to UTBreak, RSPT-v3, VMT, Crowding and LXR.
Aggregate strategies inherit it through every active branch.

## Triple

`triple_alpha_v1` evaluates UTBreak, RSPT-v3 and VMT independently.

- three same-direction signals: 100 percent risk;
- two same-direction signals: 85 percent risk;
- one signal: 55 percent risk;
- any simultaneous long/short conflict: no trade.

The selected branch keeps its own entry, stop and take-profit prices. Triple only
scales quantity, notional, margin and risk fields.

## Telegram

```text
/utbreak vmt on
/utbreak vmt off
/utbreak vmt status
/utbreak triple on
/utbreak triple off
/utbreak triple status
```
