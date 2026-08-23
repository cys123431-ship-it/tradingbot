from bot_runtime.pyramid_runtime_patch import _prepare_post_fill_guard_input


def test_filled_unprotected_is_routed_back_through_guard():
    original = {"status": "FILLED_UNPROTECTED", "reason": "audit failed"}
    guard_input, repair_status = _prepare_post_fill_guard_input(original)

    assert repair_status == "FILLED_UNPROTECTED"
    assert guard_input is not original
    assert guard_input["status"] == "ADDED"
    assert guard_input["pre_guard_status"] == "FILLED_UNPROTECTED"
    assert original["status"] == "FILLED_UNPROTECTED"


def test_order_sent_position_missing_is_routed_back_through_guard():
    original = {"status": "ORDER_SENT_POSITION_MISSING"}
    guard_input, repair_status = _prepare_post_fill_guard_input(original)

    assert repair_status == "ORDER_SENT_POSITION_MISSING"
    assert guard_input["status"] == "ADDED"


def test_normal_status_is_not_rewritten():
    original = {"status": "WAITING"}
    guard_input, repair_status = _prepare_post_fill_guard_input(original)

    assert guard_input is original
    assert repair_status is None
