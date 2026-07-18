import inspect
from pathlib import Path

import emas


def test_emas_is_a_small_composition_root():
    source = Path("emas.py").read_text(encoding="utf-8")
    assert len(source.splitlines()) < 1_000
    assert "class SignalEngine" not in source
    assert "class MainController" not in source


def test_major_runtime_responsibilities_live_in_component_modules():
    assert emas.SignalEngine.__module__ == "bot_runtime.signal_engine"
    assert emas.MainController.__module__ == "bot_runtime.controller"
    assert emas.SignalEngine.entry.__module__ == "bot_runtime.signal_entry"
    assert (
        emas.SignalEngine._place_tp_sl_orders.__module__
        == "bot_runtime.signal_exit"
    )
    assert (
        emas.SignalEngine.scan_and_trade_high_volume.__module__
        == "bot_runtime.signal_scanner"
    )
    assert (
        emas.MainController._setup_telegram.__module__
        == "bot_runtime.controller_telegram_setup"
    )


def test_extracted_descriptor_contracts_are_preserved():
    assert isinstance(emas.BaseEngine.__dict__["crypto_execution"], property)
    assert tuple(
        inspect.signature(
            emas.MainController._build_telegram_long_text_preview
        ).parameters
    ) == ("text", "max_chars", "max_lines", "suffix")
    assert isinstance(
        next(
            cls.__dict__["_is_exchange_rate_limit_error"]
            for cls in emas.SignalEngine.__mro__
            if "_is_exchange_rate_limit_error" in cls.__dict__
        ),
        staticmethod,
    )
