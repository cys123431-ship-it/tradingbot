from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding='utf-8')
    if old not in text:
        raise SystemExit(f'{label}: target block not found in {path}')
    path.write_text(text.replace(old, new, 1), encoding='utf-8')


scanner = Path('bot_runtime/signal_scanner.py')
replace_once(
    scanner,
    """    def _coin_selector_should_include_tradifi_universe(self, cfg, custom_enabled):
        if custom_enabled or self.is_upbit_mode():
            return False
        if not bool(cfg.get('include_tradifi_universe', True)):
            return False
        ctrl = getattr(self, 'ctrl', None)
        if ctrl is None or not hasattr(ctrl, 'get_exchange_mode'):
            return False
        if ctrl.get_exchange_mode() != BINANCE_MAINNET:
            return False
        return bool(self._coin_selector_tradifi_regular_session_status().get('open'))
""",
    """    def _coin_selector_should_include_tradifi_universe(self, cfg, custom_enabled):
        if custom_enabled or self.is_upbit_mode():
            return False
        if not bool(cfg.get('include_tradifi_universe', True)):
            return False
        ctrl = getattr(self, 'ctrl', None)
        if ctrl is None or not hasattr(ctrl, 'get_exchange_mode'):
            return False
        # Binance TradFi perpetuals are exchange-traded futures contracts. On
        # mainnet they remain eligible whenever Binance reports the market as
        # tradable; the underlying US cash-equity session is informational only.
        return ctrl.get_exchange_mode() == BINANCE_MAINNET
""",
    'TradFi universe eligibility',
)
replace_once(
    scanner,
    """            if self._coin_selector_is_tradifi_market(symbol, market):
                candidate['tradifi_perpetual'] = True
                candidate['tradifi_regular_session'] = dict(tradifi_session_status)
                if not tradifi_session_open:
                    candidate['accepted'] = False
                    candidate['selection_state'] = 'REJECTED'
                    candidate.setdefault('reject_reasons', []).append(
                        'REJECTED_TRADFI_REGULAR_SESSION_CLOSED'
                    )
                    candidate['analysis_error'] = self._coin_selector_tradifi_closed_reject_reason(
                        tradifi_session_status
                    )
""",
    """            if self._coin_selector_is_tradifi_market(symbol, market):
                candidate['tradifi_perpetual'] = True
                candidate['tradifi_regular_session'] = dict(tradifi_session_status)
                candidate['tradifi_24h_trading_enabled'] = True
                candidate['tradifi_session_restriction_applied'] = False
""",
    'TradFi regular-session rejection removal',
)
replace_once(
    scanner,
    """            'tradifi_universe_included': include_tradifi_universe,
            'tradifi_regular_session_open': tradifi_session_open,
            'tradifi_regular_session': dict(tradifi_session_status),
            'tradifi_candidates_considered': tradifi_candidates_considered,
""",
    """            'tradifi_universe_included': include_tradifi_universe,
            'tradifi_24h_trading_enabled': True,
            'tradifi_session_restriction_applied': False,
            'tradifi_regular_session_open': tradifi_session_open,
            'tradifi_regular_session': dict(tradifi_session_status),
            'tradifi_candidates_considered': tradifi_candidates_considered,
""",
    'TradFi report policy flags',
)

tests = Path('tests/test_utbreakout_helpers.py')
replace_once(
    tests,
    """    engine._coin_selector_tradifi_regular_session_status = lambda: {
        \"open\": False,
        \"reason\": \"outside_regular_session\",
    }
    assert engine._coin_selector_should_include_tradifi_universe(cfg, custom_enabled=False) is False
""",
    """    engine._coin_selector_tradifi_regular_session_status = lambda: {
        \"open\": False,
        \"reason\": \"outside_regular_session\",
    }
    assert engine._coin_selector_should_include_tradifi_universe(cfg, custom_enabled=False) is True
""",
    'TradFi after-hours auto-universe test',
)
replace_once(
    tests,
    'def test_coin_selector_rejects_tradifi_custom_symbol_when_regular_session_closed():',
    'def test_coin_selector_allows_tradifi_custom_symbol_when_regular_session_closed():',
    'TradFi after-hours custom-universe test name',
)
replace_once(
    tests,
    """    selected_symbols = [item.get(\"normalized_symbol\") for item in report[\"selected\"]]
    assert selected_symbols == [\"BTC/USDT\"]
    assert report[\"tradifi_regular_session_open\"] is False
    assert report[\"reject_counts\"][\"REJECTED_TRADFI_REGULAR_SESSION_CLOSED\"] == 1
""",
    """    selected_symbols = {item.get(\"normalized_symbol\") for item in report[\"selected\"]}
    assert selected_symbols == {\"BTC/USDT\", \"QQQ/USDT\"}
    qqq = next(item for item in report[\"selected\"] if item.get(\"normalized_symbol\") == \"QQQ/USDT\")
    assert qqq[\"tradifi_perpetual\"] is True
    assert qqq[\"tradifi_24h_trading_enabled\"] is True
    assert qqq[\"tradifi_session_restriction_applied\"] is False
    assert report[\"tradifi_regular_session_open\"] is False
    assert report[\"tradifi_24h_trading_enabled\"] is True
    assert report[\"tradifi_session_restriction_applied\"] is False
    assert report[\"reject_counts\"].get(\"REJECTED_TRADFI_REGULAR_SESSION_CLOSED\", 0) == 0
""",
    'TradFi after-hours selection assertions',
)
