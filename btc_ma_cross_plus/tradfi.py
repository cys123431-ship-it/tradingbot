"""Shared TradFi scope helpers (mainnet-only, US equities + gold, US session gate).

Used by both the /btcmacross (``btc_ma_cross``) and /macross
(``btc_ma_cross_plus``) strategies so the classification and the session gate
cannot drift apart.  Pure functions plus one mixin; no I/O here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from trading_safety.market_session import us_equity_regular_session_status
from utbreakout.coinselector import market_is_tradifi_perpetual

logger = logging.getLogger(__name__)

TRADFI_SCOPE = "tradfi"
MAINNET_NETWORK = "mainnet"
TRADFI_MAINNET_ONLY_REASON = "TRADFI_MAINNET_ONLY"
TRADFI_SESSION_CLOSED_REASON = "TRADFI_US_SESSION_CLOSED"

# Gold contracts are allowed; the other physical commodities are not.
TRADFI_GOLD_BASES = frozenset({
    "XAU", "GOLD", "GC", "GLD", "IAU", "XAUT", "PAXG", "GOLDTRACKER",
})
TRADFI_GOLD_MARKERS = frozenset({
    "GOLD", "GOLDSPOT", "GOLDTRACKER", "PRECIOUSMETALGOLD",
})
TRADFI_NON_GOLD_MARKERS = frozenset({
    "SILVER", "XAG", "PLATINUM", "XPT", "PALLADIUM", "XPD", "COPPER", "HG",
    "OIL", "WTI", "BRENT", "CRUDE", "NATGAS", "NATURALGAS", "ENERGY",
    "ALUMINUM", "ALUMINIUM", "NICKEL", "ZINC", "LEAD", "TIN", "IRON",
    "LITHIUM", "COBALT", "URANIUM", "SILVERSPOT",
})
TRADFI_EQUITY_MARKERS = frozenset({
    "STOCK", "STOCKS", "EQUITY", "EQUITIES", "INDEX", "INDICES", "ETF", "SHARE", "SHARES",
})


def _market_base_tokens(symbol, market):
    """Uppercase alphanumeric base candidates of a market (XAUUSDT -> XAU)."""
    market = market if isinstance(market, dict) else {}
    info = market.get("info") if isinstance(market.get("info"), dict) else {}
    raw = {
        str(market.get("base") or ""),
        str(info.get("baseAsset") or ""),
        str(info.get("underlying") or ""),
        str(symbol or "").split(":", 1)[0].split("/")[0],
    }
    return {
        "".join(ch for ch in value.upper() if ch.isalnum())
        for value in raw
        if value.strip()
    }


def market_is_tradfi_gold(symbol, market):
    """True for TradFi gold contracts (XAU/GOLD/GC/GLD/...), never silver or oil."""
    if not market_is_tradifi_perpetual(symbol, market):
        return False
    info = (market or {}).get("info") if isinstance((market or {}).get("info"), dict) else {}
    base_tokens = _market_base_tokens(symbol, market)
    gold_bases = base_tokens & TRADFI_GOLD_BASES
    if base_tokens & TRADFI_NON_GOLD_MARKERS:
        return False
    if gold_bases:
        return True
    metadata = set()
    for source in (market or {}, info):
        if not isinstance(source, dict):
            continue
        for key in ("underlyingType", "underlyingSubType", "assetType", "category",
                    "sector", "subType", "productType", "name", "displayName"):
            value = source.get(key)
            token = "".join(ch for ch in str(value or "").upper() if ch.isalnum())
            if not token:
                continue
            metadata.add(token)
            if any(marker in token for marker in TRADFI_NON_GOLD_MARKERS):
                return False
    if metadata & TRADFI_GOLD_MARKERS or any("GOLD" in token for token in metadata):
        return True
    return bool(gold_bases)


def market_is_tradfi_us_equity(symbol, market):
    """True for a TradFi US-equity contract (not a commodity)."""
    if not market_is_tradifi_perpetual(symbol, market):
        return False
    if market_is_tradfi_gold(symbol, market):
        return False
    info = (market or {}).get("info") if isinstance((market or {}).get("info"), dict) else {}
    metadata = set()
    for source in (market or {}, info):
        if not isinstance(source, dict):
            continue
        for key in ("underlyingType", "underlyingSubType", "assetType", "category",
                    "sector", "subType", "productType"):
            value = source.get(key)
            token = "".join(ch for ch in str(value or "").upper() if ch.isalnum())
            if token:
                metadata.add(token)
    if metadata & TRADFI_EQUITY_MARKERS:
        return True
    base_tokens = _market_base_tokens(symbol, market)
    return not (base_tokens & TRADFI_NON_GOLD_MARKERS)


def market_in_tradfi_scope(symbol, market):
    """TradFi scope membership: US equity or gold only."""
    return market_is_tradfi_us_equity(symbol, market) or market_is_tradfi_gold(symbol, market)


class TradFiScopeMixin:
    """Mainnet-only TradFi scope: universe + US regular-session gate.

    Expects the host service to provide ``_clock()``, ``now_ms()``,
    ``exchange(network)``, ``_call(fn, *args)`` and ``_universe_cache``.
    """

    @staticmethod
    def tradfi_network_allowed(network):
        """TradFi markets exist on the Binance mainnet only."""
        return str(network or "").lower() == MAINNET_NETWORK

    def us_session_status(self):
        """US regular-session snapshot (America/New_York, holidays + early close)."""
        try:
            now = datetime.fromtimestamp(self._clock(), timezone.utc)
            return dict(us_equity_regular_session_status(now))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("TradFi US session lookup failed: %s", exc)
            return {"open": False, "reason": f"session_unavailable: {type(exc).__name__}"}

    def tradifi_scope_state(self, network):
        """Why the TradFi scope is active/blocked right now (Telegram + guard)."""
        if not self.tradfi_network_allowed(network):
            return {"allowed": False, "reason": TRADFI_MAINNET_ONLY_REASON,
                    "detail": "TradFi는 바이낸스 메인넷에서만 지원됩니다."}
        session = self.us_session_status()
        if not session.get("open"):
            return {"allowed": False, "reason": TRADFI_SESSION_CLOSED_REASON,
                    "detail": f"미국 정규장 마감 ({session.get('reason')})", "session": session}
        return {"allowed": True, "reason": "", "detail": "", "session": session}

    def tradfi_entry_block(self, network, cfg):
        """Reason new TradFi entries are blocked now ('' when allowed)."""
        if cfg.get("scan_scope") != TRADFI_SCOPE:
            return ""
        state = self.tradifi_scope_state(network)
        return "" if state.get("allowed") else str(state.get("reason") or TRADFI_MAINNET_ONLY_REASON)

    async def _tradfi_universe(self, network, cfg):
        """TradFi perpetuals on mainnet: US equities plus gold, by 24h volume."""
        if not self.tradfi_network_allowed(network):
            raise ValueError(TRADFI_MAINNET_ONLY_REASON)
        cache_key = f"tradfi:{network}"
        cached = self._universe_cache.get(cache_key)
        if cached and self.now_ms() - cached[0] < 600_000:
            return cached[1]
        exchange = await self.exchange(network)
        markets = getattr(exchange, "markets", None) or await self._call(exchange.load_markets)
        tickers = await self._call(exchange.fetch_tickers)
        rows = []
        for symbol, market in (markets or {}).items():
            if not isinstance(market, dict) or not isinstance(tickers, dict):
                continue
            if not market_in_tradfi_scope(symbol, market):
                continue
            if market.get("active") is False:
                continue
            status = str((market.get("info") or {}).get("status") or "TRADING").upper()
            if status != "TRADING":
                continue
            ticker = (tickers or {}).get(symbol) or {}
            info = ticker.get("info") if isinstance(ticker.get("info"), dict) else {}
            try:
                volume = float(ticker.get("quoteVolume") if ticker.get("quoteVolume") is not None
                               else info.get("quoteVolume") or 0.0)
            except (TypeError, ValueError):
                volume = 0.0
            market_id = str(market.get("id") or "").upper() or symbol.split(":")[0].replace("/", "")
            rows.append((symbol, market_id, volume))
        rows.sort(key=lambda row: (-row[2], row[1]))
        universe = rows[: int(cfg["alt_universe_size"])]
        if not universe:
            raise ValueError("empty tradfi universe")
        self._universe_cache[cache_key] = (self.now_ms(), universe, len(universe))
        return universe


__all__ = (
    "MAINNET_NETWORK",
    "TRADFI_GOLD_BASES",
    "TRADFI_MAINNET_ONLY_REASON",
    "TRADFI_SCOPE",
    "TRADFI_SESSION_CLOSED_REASON",
    "TradFiScopeMixin",
    "market_in_tradfi_scope",
    "market_is_tradfi_gold",
    "market_is_tradfi_us_equity",
)
