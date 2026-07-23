import asyncio
from types import SimpleNamespace

from bot_runtime.signal_automatic_controls import SignalAutomaticControlsMixin


class _DB:
    def __init__(self, count):
        self.count = count

    def get_daily_automatic_entry_count(self):
        return self.count


class _LegacyBreakoutStatusMixin:
    async def _calculate_utbot_filtered_breakout_signal(
        self,
        symbol,
        df,
        strategy_params,
        *,
        force_reprocess=False,
    ):
        daily_entries = self.get_automatic_daily_entry_count()
        stale_limit = 5
        if daily_entries >= stale_limit:
            reason = (
                "REJECTED_DAILY_LOSS_LIMIT: daily trade count "
                f"{daily_entries} >= {stale_limit}"
            )
            return None, reason, {
                "reason": reason,
                "reject_code": "REJECTED_DAILY_LOSS_LIMIT",
            }
        return "short", "accepted", {"accepted_side": "short"}


class _Engine(SignalAutomaticControlsMixin, _LegacyBreakoutStatusMixin):
    def __init__(self, count, effective_limit):
        self.db = _DB(count)
        self.ctrl = SimpleNamespace(
            get_effective_automatic_daily_trade_limit=lambda: effective_limit,
        )
        self.last_entry_reason = {}
        self.saved_status = {}

    def _store_utbot_filtered_breakout_status(self, symbol, status):
        self.saved_status[symbol] = dict(status)


def test_extended_limit_prevents_legacy_five_trade_guard_from_blocking():
    engine = _Engine(count=5, effective_limit=10)

    signal, reason, _ = asyncio.run(
        engine._calculate_utbot_filtered_breakout_signal("GOOGL/USDT:USDT", None, {})
    )

    assert signal == "short"
    assert reason == "accepted"
    assert int(engine.get_automatic_daily_entry_count()) == 5


def test_extended_limit_blocks_at_ten_and_uses_trade_limit_reject_code():
    engine = _Engine(count=10, effective_limit=10)

    signal, reason, status = asyncio.run(
        engine._calculate_utbot_filtered_breakout_signal("GOOGL/USDT:USDT", None, {})
    )

    assert signal is None
    assert reason == "REJECTED_DAILY_TRADE_LIMIT: daily trade count 10 >= 10"
    assert status["reject_code"] == "REJECTED_DAILY_TRADE_LIMIT"
    assert engine.saved_status["GOOGL/USDT:USDT"]["reason"] == reason
    assert engine.last_entry_reason["GOOGL/USDT:USDT"] == reason


def test_default_limit_still_blocks_at_five():
    engine = _Engine(count=5, effective_limit=5)

    signal, reason, status = asyncio.run(
        engine._calculate_utbot_filtered_breakout_signal("BTC/USDT:USDT", None, {})
    )

    assert signal is None
    assert reason == "REJECTED_DAILY_TRADE_LIMIT: daily trade count 5 >= 5"
    assert status["reject_code"] == "REJECTED_DAILY_TRADE_LIMIT"
