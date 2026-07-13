from pathlib import Path

EMAS = Path("emas.py")
RESTRICTED_TEST = Path("tests/test_utbreakout_restricted_symbols.py")
COINSELECTOR_TEST = Path("tests/test_coinselector.py")

text = EMAS.read_text(encoding="utf-8")

old_block = '''    def _utbreakout_restricted_symbol_bases(self):
        """Account-region exclusions supplied by the user; never scan or trade."""
        return {'SKHYNIX', 'HYUNDAI', 'SAMSUNG'}

    def _utbreakout_symbol_base(self, symbol):'''
new_block = '''    def _utbreakout_restricted_symbol_bases(self):
        """Account-region exclusions supplied by the user; never scan or trade."""
        return {'SKHYNIX', 'HYUNDAI', 'SAMSUNG'}

    def _utbreakout_restricted_symbol_exceptions(self):
        """Explicitly allowed Binance contracts that must not match legacy names."""
        # SKHY is the Nasdaq ADR-linked Binance TradFi perpetual. It is a
        # different contract from the legacy SKHYNIX synthetic ticker.
        return {'SKHY'}

    def _utbreakout_symbol_base(self, symbol):'''

old_check = '''    def _is_utbreakout_restricted_symbol(self, symbol):
        return (
            self._utbreakout_symbol_base(symbol)
            in self._utbreakout_restricted_symbol_bases()
        )'''
new_check = '''    def _is_utbreakout_restricted_symbol(self, symbol):
        base = self._utbreakout_symbol_base(symbol)
        if base in self._utbreakout_restricted_symbol_exceptions():
            return False
        return base in self._utbreakout_restricted_symbol_bases()'''

if old_block not in text:
    raise SystemExit("restricted symbol block not found")
if old_check not in text:
    raise SystemExit("restricted symbol predicate not found")

text = text.replace(old_block, new_block, 1)
text = text.replace(old_check, new_check, 1)
text = text.replace(
    "제외 티커: `SKHYNIX, HYUNDAI, SAMSUNG`",
    "제외 티커: `SKHYNIX, HYUNDAI, SAMSUNG` (SKHY ADR perpetual은 허용)",
    1,
)
EMAS.write_text(text, encoding="utf-8")

restricted = RESTRICTED_TEST.read_text(encoding="utf-8")
needle = '''    assert not engine._is_utbreakout_restricted_symbol("SOLUSDT")
    assert not engine._is_utbreakout_restricted_symbol("BTC/USDT:USDT")
    assert not engine._is_utbreakout_restricted_symbol("ETHUSDT")'''
replacement = '''    for symbol in (
        "SKHY",
        "SKHYUSDT",
        "SKHY/USDT:USDT",
    ):
        assert not engine._is_utbreakout_restricted_symbol(symbol)

    assert not engine._is_utbreakout_restricted_symbol("SOLUSDT")
    assert not engine._is_utbreakout_restricted_symbol("BTC/USDT:USDT")
    assert not engine._is_utbreakout_restricted_symbol("ETHUSDT")'''
if needle not in restricted:
    raise SystemExit("restricted symbol test insertion point not found")
restricted = restricted.replace(needle, replacement, 1)
RESTRICTED_TEST.write_text(restricted, encoding="utf-8")

coinselector = COINSELECTOR_TEST.read_text(encoding="utf-8")
test_name = "test_coinselector_accepts_skhy_tradifi_perpetual"
if test_name not in coinselector:
    insert_after = '''def test_coinselector_accepts_tradifi_usdt_perpetual():
    cfg = default_coin_selector_config()
    candidate = build_base_candidate(
        "EWY/USDT:USDT",
        _ticker(quoteVolume=500_000_000),
        _market(info={"contractType": "TRADIFI_PERPETUAL", "status": "TRADING"}),
        cfg,
    )

    assert candidate["accepted"] is True
    assert candidate["tradifi_perpetual"] is True
    assert market_is_tradifi_perpetual("EWY/USDT:USDT", _market(info={"contractType": "TRADIFI_PERPETUAL", "status": "TRADING"})) is True
    assert "INVALID_MARKET" not in candidate["reject_reasons"]
'''
    addition = insert_after + '''\n\ndef test_coinselector_accepts_skhy_tradifi_perpetual():
    cfg = default_coin_selector_config()
    market = _market(
        symbol="SKHY/USDT:USDT",
        id="SKHYUSDT",
        base="SKHY",
        info={
            "symbol": "SKHYUSDT",
            "contractType": "TRADIFI_PERPETUAL",
            "status": "TRADING",
        },
    )
    candidate = build_base_candidate(
        "SKHY/USDT:USDT",
        _ticker(quoteVolume=500_000_000),
        market,
        cfg,
    )

    assert candidate["accepted"] is True
    assert candidate["tradifi_perpetual"] is True
    assert market_is_tradifi_perpetual("SKHY/USDT:USDT", market) is True
    assert "INVALID_MARKET" not in candidate["reject_reasons"]
'''
    if insert_after not in coinselector:
        raise SystemExit("coin selector test insertion point not found")
    coinselector = coinselector.replace(insert_after, addition, 1)
COINSELECTOR_TEST.write_text(coinselector, encoding="utf-8")
