"""Telegram commands must not collide: only the first matching handler ever runs."""
from collections import Counter

from trading_safety.sma_periods import BTC_MA_PERIOD_COMMANDS, MA_CROSS_PERIOD_COMMANDS


class _App:
    def __init__(self):
        self.handlers = []

    def add_handler(self, handler, group=0):
        self.handlers.append((group, handler))


def _commands(app):
    names = []
    for group, handler in app.handlers:
        for name in getattr(handler, "commands", ()) or ():
            names.append((group, name))
    return names


def _registered(*registrars):
    from bot_runtime.controller_btc_ma_cross import ControllerBtcMaCrossMixin
    from bot_runtime.controller_btc_pullback import ControllerBtcPullbackMixin
    from bot_runtime.controller_ma_cross import ControllerMaCrossMixin

    class Controller(ControllerBtcMaCrossMixin, ControllerMaCrossMixin, ControllerBtcPullbackMixin):
        pass

    controller = Controller()
    controller.tg_app = _App()
    for name in registrars:
        getattr(controller, name)(lambda fn: fn)
    return controller.tg_app


def test_period_command_sets_are_disjoint():
    assert not set(BTC_MA_PERIOD_COMMANDS) & set(MA_CROSS_PERIOD_COMMANDS)
    assert "btcma" in BTC_MA_PERIOD_COMMANDS and "map" in MA_CROSS_PERIOD_COMMANDS


def test_no_command_is_registered_twice_by_the_strategy_menus():
    app = _registered("_register_btc_pullback_handlers", "_register_btc_ma_cross_handlers",
                      "_register_ma_cross_handlers")
    duplicates = [key for key, count in Counter(_commands(app)).items() if count > 1]
    assert duplicates == []


def test_map_goes_to_macross_and_btcma_to_btcmacross():
    app = _registered("_register_btc_ma_cross_handlers", "_register_ma_cross_handlers")
    owner = {}
    for _, handler in app.handlers:
        for name in getattr(handler, "commands", ()) or ():
            owner.setdefault(name, handler.callback.__qualname__)
    assert "btc_ma_period_cmd" in owner["btcma"] and "btc_ma_period_cmd" in owner["btcmaperiod"]
    assert "ma_cross_period_cmd" in owner["map"] and "btc_ma" not in owner["map"]
    assert "ma_cross_period_cmd" in owner["maperiod"] and "btc_ma" not in owner["maperiod"]
