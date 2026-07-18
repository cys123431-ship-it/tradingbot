from datetime import datetime
import inspect
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from telegram import InlineKeyboardButton

import emas


def _callbacks(markup_or_rows):
    rows = getattr(markup_or_rows, "inline_keyboard", markup_or_rows)
    return [[button.callback_data for button in row] for row in rows]


def _texts(markup_or_rows):
    rows = getattr(markup_or_rows, "inline_keyboard", markup_or_rows)
    return [[button.text for button in row] for row in rows]


def test_utbreakout_inline_keyboard_gets_risk_buttons_before_watchlist():
    rows = [
        [
            InlineKeyboardButton("UTBreak ON", callback_data="utb:on"),
            InlineKeyboardButton("UTBreak OFF", callback_data="utb:off"),
        ],
        [InlineKeyboardButton("DUAL STATUS", callback_data="utb:dual_status")],
        [InlineKeyboardButton("coin watchlist", callback_data="utb:watchlist")],
    ]

    updated = emas.append_live_real_risk_buttons_to_utbreakout_rows(rows)

    assert _callbacks(updated)[-2] == ["risk:menu", "risk:status"]
    assert _callbacks(updated)[-1] == ["utb:watchlist"]
    assert _texts(updated)[-2] == ["리스크 설정", "리스크 상태"]

    unchanged = emas.append_live_real_risk_buttons_to_utbreakout_rows(updated)
    assert _callbacks(unchanged).count(["risk:menu", "risk:status"]) == 1


def test_live_real_risk_menu_and_status_keyboards_have_expected_callbacks():
    menu = emas.build_live_real_risk_menu_keyboard()
    status = emas.build_live_real_risk_status_keyboard()
    confirm = emas.build_live_real_risk_confirm_keyboard(0.05)

    assert _callbacks(menu) == [
        ["risk:set:0.0025", "risk:set:0.005", "risk:set:0.01"],
        ["risk:set:0.02", "risk:set:0.03", "risk:set:0.05"],
        ["risk:set:0.10", "risk:safe", "risk:manual"],
        ["risk:back:utbreak"],
    ]
    assert _callbacks(status) == [["risk:menu"], ["risk:back:utbreak"]]
    assert _callbacks(confirm) == [["risk:confirm:set:0.05", "risk:cancel"]]


def test_live_real_risk_status_text_includes_budget_caps_and_kst_history():
    cfg = {
        "live_real_risk_pct_user": 0.005,
        "default_real_risk_pct": 0.005,
        "min_real_risk_pct": 0.001,
        "max_real_risk_pct": 0.05,
        "max_risk_pct_increases_per_day": 2,
        "risk_pct_change_reset_timezone": "Asia/Seoul",
    }
    state = {
        "risk_pct_increases_today": 1,
        "risk_pct_change_history": [
            {
                "at": "2026-07-09T09:12:00+09:00",
                "from": 0.005,
                "to": 0.01,
                "source": "button",
            }
        ],
    }
    limits = emas.resolve_live_small_cap_limits(cfg, account_equity=164.0)

    text = emas.render_live_real_risk_status_text(
        cfg,
        state,
        limits,
        equity_source="reference",
    )

    assert "0.50%" in text
    assert "0.005000" in text
    assert "164.00 USDT" in text
    assert "reference equity" in text
    assert "KST" in text
    assert "0.50% -> 1.00%" in text


def test_button_risk_change_uses_same_limit_state_and_history_as_risk_command():
    cfg = {
        "max_risk_pct_increases_per_day": 2,
        "risk_pct_change_reset_timezone": "Asia/Seoul",
        "_risk_change_source": "button",
    }
    now = datetime(2026, 7, 9, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    state = {}

    first = emas.apply_live_real_risk_pct_change(state, 0.005, 0.01, cfg, now=now)
    second = emas.apply_live_real_risk_pct_change(first["state"], 0.01, 0.02, cfg, now=now)
    blocked = emas.apply_live_real_risk_pct_change(second["state"], 0.02, 0.03, cfg, now=now)
    same = emas.apply_live_real_risk_pct_change(second["state"], 0.02, 0.02, cfg, now=now)

    assert first["allowed"] is True
    assert second["allowed"] is True
    assert blocked["allowed"] is False
    assert blocked["reason"] == "daily_risk_increase_limit"
    assert same["allowed"] is True
    assert same["changed"] is False
    assert second["state"]["risk_pct_increases_today"] == 2
    assert second["state"]["risk_pct_change_history"][-1]["source"] == "button"


def test_main_reply_keyboard_does_not_add_risk_button():
    source = inspect.getsource(emas.MainController._build_main_keyboard)
    main_menu_start = source.index("[KeyboardButton(\"🚨 STOP\")")
    main_menu_end = source.index("return ReplyKeyboardMarkup(kb, resize_keyboard=True)", main_menu_start)
    main_menu_source = source[main_menu_start:main_menu_end]

    assert 'KeyboardButton("/utbreak")' in main_menu_source
    assert 'KeyboardButton("/risk")' not in main_menu_source


def test_utbreakout_menu_uses_integrated_five_strategy_selector():
    source = inspect.getsource(emas.MainController._setup_telegram)
    menu_start = source.index("def _build_utbreakout_keyboard():")
    menu_end = source.index("async def _edit_utbreakout_menu", menu_start)
    menu_source = source[menu_start:menu_end]

    assert 'callback_data="utb:quad:on"' in menu_source
    assert 'callback_data="utb:quad:off"' in menu_source
    assert 'callback_data="utb:quad_status"' in menu_source
    assert 'callback_data="utb:qselect"' in menu_source
    assert 'callback_data="utb:rsp:on"' not in menu_source
    assert 'callback_data="utb:qh:on"' not in menu_source
    assert 'callback_data="utb:crowding:on"' not in menu_source
    assert 'callback_data="utb:mtrend:on"' not in menu_source


def test_order_path_summary_distinguishes_live_demo_and_dry_run():
    live = emas.build_utbreakout_order_path_summary(
        {
            "exchange_mode": "BINANCE_FUTURES_MAINNET",
            "live_activation_stage": "LIVE_REAL_SMALL_CAP",
            "real_order_enabled": True,
            "live_trading": True,
            "testnet": False,
            "live_parity_signal_enabled": True,
        }
    )
    demo = emas.build_utbreakout_order_path_summary(
        {
            "exchange_mode": emas.BINANCE_TESTNET,
            "live_activation_stage": "TESTNET_ONLY",
            "testnet": True,
        }
    )
    dry = emas.build_utbreakout_order_path_summary(
        {"exchange_mode": "BINANCE_FUTURES_MAINNET"},
        micro_cfg={"enabled": True, "dry_run": True},
    )

    assert live["live_order_enabled"] is True
    assert live["order_action"] == "ready_to_order"
    assert demo["demo_order_enabled"] is True
    assert demo["order_action"] == "testnet_or_demo_order"
    assert dry["dry_run"] is True
    assert dry["order_action"] == "signal_only"
    assert "live_order_enabled=true" in emas.format_utbreakout_order_path_summary(live)


def test_market_quality_strict_blocks_short_adverse_funding_but_balanced_reduces():
    engine = object.__new__(emas.SignalEngine)
    values = {
        "atr_pct": 0.5,
        "funding_rate": -0.0015,
        "futures_spread_pct": 0.02,
    }
    strict_cfg = emas.build_default_utbot_filtered_breakout_config()
    strict_cfg["utbreak_entry_relaxation_mode"] = "strict"
    balanced_cfg = emas.build_default_utbot_filtered_breakout_config()
    balanced_cfg["utbreak_entry_relaxation_mode"] = "balanced"

    strict = engine._evaluate_utbreakout_market_quality("short", strict_cfg, values)
    balanced = engine._evaluate_utbreakout_market_quality("short", balanced_cfg, values)

    assert strict["hard_block"] is True
    assert strict["diagnostics"]["funding_action"] == "block"
    assert balanced["hard_block"] is False
    assert balanced["state"] == "reduced"
    assert balanced["diagnostics"]["funding_action"] == "size_reduce"


def test_market_quality_balanced_keeps_extreme_short_adverse_funding_blocked():
    engine = object.__new__(emas.SignalEngine)
    cfg = emas.build_default_utbot_filtered_breakout_config()
    cfg["utbreak_entry_relaxation_mode"] = "balanced"

    result = engine._evaluate_utbreakout_market_quality(
        "short",
        cfg,
        {
            "atr_pct": 0.5,
            "funding_rate": -0.0030,
            "futures_spread_pct": 0.02,
        },
    )

    assert result["hard_block"] is True
    assert result["diagnostics"]["funding_action"] == "block"
