from bot_runtime.controller_exchange import ControllerExchangeMixin


def _controller():
    controller = ControllerExchangeMixin()
    controller._telegram_event_alerts_only = lambda: True
    return controller


def test_event_only_mode_keeps_exit_and_protection_lifecycle_messages():
    controller = _controller()

    assert controller._should_suppress_telegram_notice(
        "⚠️ BTC/USDT Soft structure stop 사유로 reduceOnly 시장가 청산 실행"
    ) is False
    assert controller._should_suppress_telegram_notice(
        "🧭 UTBreak Runner SL 갱신: BTC/USDT LONG"
    ) is False
    assert controller._should_suppress_telegram_notice(
        "✅ BTC/USDT TP1 재생성 완료"
    ) is False


def test_event_only_mode_keeps_explicit_typed_event_and_suppresses_diagnostics():
    controller = _controller()

    assert controller._should_suppress_telegram_notice(
        "문구와 무관한 체결 이벤트",
        event_type="EXIT_FILLED",
    ) is False
    assert controller._should_suppress_telegram_notice(
        "🧪 UT 진단: 아직 후보 없음"
    ) is True
