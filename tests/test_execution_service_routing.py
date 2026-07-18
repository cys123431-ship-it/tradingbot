import asyncio
import ast
import inspect
import textwrap
from types import SimpleNamespace

import emas

from trading_safety.execution_service import CryptoExecutionService


class Gateway:
    def __init__(self):
        self.calls = []

    async def submit_entry(self, **kwargs):
        self.calls.append(("entry", kwargs))
        return "entry-result"

    async def submit_position_add(self, **kwargs):
        self.calls.append(("add", kwargs))
        return "add-result"

    async def submit_limit_entry_or_add(self, **kwargs):
        self.calls.append(("limit", kwargs))
        return "limit-result"

    async def submit_reduce_only_close(self, **kwargs):
        self.calls.append(("close", kwargs))
        return "close-result"


def test_execution_service_routes_all_order_operations_to_gateway():
    async def scenario():
        gateway = Gateway()
        service = CryptoExecutionService(
            SimpleNamespace(), SimpleNamespace(), gateway, algo_gateway=SimpleNamespace()
        )
        assert await service.submit_entry(symbol="BTC") == "entry-result"
        assert await service.submit_position_add(symbol="BTC") == "add-result"
        assert await service.submit_limit_entry(symbol="BTC") == "limit-result"
        assert await service.submit_reduce_only_close(symbol="BTC") == "close-result"
        assert [name for name, _ in gateway.calls] == ["entry", "add", "limit", "close"]

    asyncio.run(scenario())


def test_execution_service_routes_protection_to_shared_manager():
    async def scenario():
        calls = []

        async def manager(kind, **kwargs):
            calls.append((kind, kwargs))
            return kind

        service = CryptoExecutionService(
            SimpleNamespace(),
            SimpleNamespace(),
            Gateway(),
            algo_gateway=SimpleNamespace(),
            protection_manager=manager,
        )
        assert await service.place_stop_loss(symbol="BTC") == "sl"
        assert await service.place_take_profit(symbol="BTC") == "tp"
        assert calls == [("sl", {"symbol": "BTC"}), ("tp", {"symbol": "BTC"})]

    asyncio.run(scenario())


def _direct_exchange_order_methods(target):
    found = set()
    sources = target.__mro__ if inspect.isclass(target) else (target,)
    for source_target in sources:
        if source_target is object:
            continue
        tree = ast.parse(textwrap.dedent(inspect.getsource(source_target)))
        for function in (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            for node in ast.walk(function):
                if not isinstance(node, ast.Attribute) or node.attr != "create_order":
                    continue
                value = node.value
                if (
                    isinstance(value, ast.Attribute)
                    and value.attr == "exchange"
                    and isinstance(value.value, ast.Name)
                    and value.value.id == "self"
                ):
                    found.add(function.name)
    return found


def test_futures_engines_do_not_bypass_execution_service():
    for engine_class in (
        emas.TemaEngine,
        emas.ShannonEngine,
        emas.DualThrustEngine,
        emas.DualModeFractalEngine,
    ):
        assert _direct_exchange_order_methods(engine_class) == set()

    signal_direct = _direct_exchange_order_methods(emas.SignalEngine)
    assert signal_direct == {
        "_entry_upbit_spot",
        "_exit_upbit_spot",
        "_create_protection_order_with_retries",
    }
    assert "emergency_stop" not in _direct_exchange_order_methods(emas.MainController)
