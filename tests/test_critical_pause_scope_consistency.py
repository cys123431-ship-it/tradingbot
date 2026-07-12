import json

import pytest

import emas
from trading_safety.entry_block import canonical_futures_symbol


@pytest.mark.parametrize("scope", ["GLOBAL", "global", None, "UNKNOWN"])
def test_global_scope_variants_fail_closed(scope):
    state = {
        "status": "CRITICAL_PAUSED",
        "scope": scope,
        "reason": "test",
        "origin_symbol": "BTC/USDT:USDT",
    }
    decision = emas.evaluate_critical_pause_block(state=state, requested_symbol="ETH/USDT:USDT")
    assert decision.blocked
    assert decision.scope == "GLOBAL"


def test_symbol_scope_and_malformed_origin():
    good = {
        "status": "CRITICAL_PAUSED",
        "scope": "SYMBOL",
        "reason": "test",
        "origin_symbol": "BTC/USDT:USDT",
    }
    assert emas.evaluate_critical_pause_block(state=good, requested_symbol="BTCUSDT").blocked
    assert not emas.evaluate_critical_pause_block(state=good, requested_symbol="ETHUSDT").blocked

    for bad_origin in (None, "", "*", "@@@"):
        bad = dict(good, origin_symbol=bad_origin)
        decision = emas.evaluate_critical_pause_block(state=bad, requested_symbol="ETHUSDT")
        assert decision.blocked
        assert decision.scope == "GLOBAL"
        assert decision.origin_symbol == "*"


def test_loader_malformed_and_legacy(tmp_path, monkeypatch):
    path = tmp_path / "pause.json"
    monkeypatch.setattr(emas, "PAUSE_STATE_FILE", str(path))
    for payload in (None, [], "text", {}, {"status": "RUNNING"}):
        path.write_text(json.dumps(payload), encoding="utf-8")
        state = emas.load_critical_pause_state()
        assert state["status"] == "CRITICAL_PAUSED"
        assert state["scope"] == "GLOBAL"
        assert state["pause_id"].startswith("unreadable:")

    legacy = {"symbol": "BTC/USDT:USDT", "reason": "test"}
    path.write_text(json.dumps(legacy), encoding="utf-8")
    first = emas.load_critical_pause_state()
    second = emas.load_critical_pause_state()
    assert first["origin_symbol"] == "BTC/USDT:USDT"
    assert first["pause_id"] == second["pause_id"]
    assert first["pause_id"].startswith("legacy:")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BTCUSDT", "BTC/USDT:USDT"),
        ("BTC/USDT", "BTC/USDT:USDT"),
        ("BTC/USDT:USDT", "BTC/USDT:USDT"),
        ("ETHUSDC", "ETH/USDC:USDC"),
        ("ETH/USDC:USDC", "ETH/USDC:USDC"),
        ("1000SHIBUSDT", "1000SHIB/USDT:USDT"),
        ("HMSTR/USDT:USDT", "HMSTR/USDT:USDT"),
    ],
)
def test_canonicalizer_regression(raw, expected):
    assert canonical_futures_symbol(raw) == expected
