import emas


def _engine():
    engine = object.__new__(emas.SignalEngine)
    engine.safe_price = lambda _symbol, value: f"{float(value):.4f}"
    engine.safe_amount = lambda _symbol, value: f"{float(value):.3f}"
    return engine


def _protection_id(engine, *, kind="sl", trigger=90.0, qty=1.0, leg="SL", identity="entry-1"):
    return engine._build_protection_client_order_id(
        "BTC/USDT:USDT",
        "long",
        kind,
        {"entryPrice": 100.0, "contracts": qty},
        trigger_price=trigger,
        quantity=qty,
        leg=leg,
        position_identity=identity,
    )


def test_protection_id_is_stable_across_engine_instances_and_wall_clock(monkeypatch):
    monkeypatch.setattr(emas.time, "time", lambda: 1.0)
    first = _protection_id(_engine())
    monkeypatch.setattr(emas.time, "time", lambda: 9999999999.0)
    assert first == _protection_id(_engine())


def test_protection_identity_changes_for_material_order_changes():
    engine = _engine()
    values = {
        _protection_id(engine),
        _protection_id(engine, trigger=91.0),
        _protection_id(engine, qty=2.0),
        _protection_id(engine, kind="tp", leg="TP1", trigger=110.0),
        _protection_id(engine, kind="tp", leg="TP2", trigger=120.0),
        _protection_id(engine, identity="entry-2"),
    }
    assert len(values) == 6
