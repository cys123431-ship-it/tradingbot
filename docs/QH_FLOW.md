# QH-Flow, L2 Gate and Triple

## QH-Flow

`qh_flow_v1` evaluates Binance USDT perpetual markets at each quarter-hour boundary.
It fetches aggregate trades for the first 10 seconds of the current boundary and
compares them with the same 10-second window from the previous eight boundaries.
No persistent collector or historical database is required.

The live decision requires:

- sufficient Taker notional and trade count;
- directional Taker imbalance;
- imbalance z-score above the configured threshold;
- abnormal notional and trade-count ratios;
- price retention in the same direction;
- a non-stressed L2 order book;
- no severe funding, basis, or long/short crowding.

Accepted entries use the existing risk plan, margin cap, single-position guard,
order gateway, TP/SL protection, reconciliation and Telegram reporting paths.

## Shared L2 Gate

The shared L2 gate evaluates the top 20 bid and ask levels.

- `CALM`: normal risk.
- `MIXED`: reduced risk, default 65 percent.
- `STRESSED`: new entries blocked.

The gate is applied to UTBreak, RSPT-v2 and QH-Flow. Dual inherits it through its
two branches. Triple inherits it through all three independent branches.

## QH confirmation for UTBreak and RSPT-v2

When a UTBreak or RSPT entry appears within three minutes before a quarter-hour
boundary, the entry waits for the first 10-second QH window. A same-direction QH
signal confirms normal risk, an opposite signal blocks the entry, and no accepted
QH signal reduces risk to 60 percent. Outside the confirmation window the original
strategy proceeds normally.

## Triple

`triple_alpha_v1` evaluates UTBreak, RSPT-v2 and QH-Flow independently.

- three same-direction signals: 100 percent risk;
- two same-direction signals: 85 percent risk;
- one signal: 55 percent risk;
- any simultaneous long/short conflict: no trade.

The selected branch keeps its own entry, stop and take-profit prices. Triple only
scales quantity, notional, margin and risk fields.

## Telegram

```text
/utbreak qh on
/utbreak qh off
/utbreak qh status
/utbreak triple on
/utbreak triple off
/utbreak triple status
```
