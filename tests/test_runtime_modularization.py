import builtins
import dis
import inspect
import sys
import types
from pathlib import Path

import emas
from bot_runtime.controller import REPOSITORY_ROOT


def test_emas_is_a_small_composition_root():
    source = Path("emas.py").read_text(encoding="utf-8")
    assert len(source.splitlines()) < 1_000
    assert "class SignalEngine" not in source
    assert "class MainController" not in source


def test_controller_runtime_paths_stay_anchored_to_repository_root():
    repository_root = Path(__file__).resolve().parent.parent
    assert REPOSITORY_ROOT == repository_root
    assert "self.base_dir = str(REPOSITORY_ROOT)" in inspect.getsource(
        emas.MainController.__init__
    )


def test_extracted_runtime_globals_are_resolved():
    def nested_code_objects(code):
        yield code
        for value in code.co_consts:
            if isinstance(value, types.CodeType):
                yield from nested_code_objects(value)

    missing = set()
    seen = set()
    for module_name, module in tuple(sys.modules.items()):
        if not module_name.startswith("bot_runtime."):
            continue
        candidates = []
        for value in tuple(vars(module).values()):
            if inspect.isfunction(value):
                candidates.append(value)
            elif inspect.isclass(value) and value.__module__ == module_name:
                for descriptor in vars(value).values():
                    if isinstance(descriptor, (staticmethod, classmethod)):
                        descriptor = descriptor.__func__
                    elif isinstance(descriptor, property):
                        candidates.extend(
                            accessor
                            for accessor in (
                                descriptor.fget,
                                descriptor.fset,
                                descriptor.fdel,
                            )
                            if accessor is not None
                        )
                        continue
                    if inspect.isfunction(descriptor):
                        candidates.append(descriptor)

        for proxy in candidates:
            function = getattr(proxy, "__runtime_original__", proxy)
            if function.__module__ != module_name or id(function) in seen:
                continue
            seen.add(id(function))
            for code in nested_code_objects(function.__code__):
                for instruction in dis.get_instructions(code):
                    if instruction.opname not in {"LOAD_GLOBAL", "LOAD_NAME"}:
                        continue
                    name = instruction.argval
                    if name not in function.__globals__ and not hasattr(builtins, name):
                        missing.add((module_name, name))

    # This optional archived engine is guarded by DUAL_MODE_AVAILABLE.
    missing.discard(("bot_runtime.legacy_engines", "DualModeFractalStrategy"))
    assert not missing


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
