import pytest

from scripts import live_real_trade


def test_cli_requires_once_before_running(monkeypatch):
    async def should_not_run(args):
        raise AssertionError("run_once must not be called without --once")

    monkeypatch.setattr(live_real_trade, "run_once", should_not_run)

    with pytest.raises(SystemExit):
        live_real_trade.main([
            "--symbol",
            "BTC/USDT:USDT",
            "--confirm",
            live_real_trade.LIVE_REAL_CONFIRM_TEXT,
        ])


def test_cli_requires_exact_confirmation_before_running(monkeypatch):
    async def should_not_run(args):
        raise AssertionError("run_once must not be called with a bad confirmation")

    monkeypatch.setattr(live_real_trade, "run_once", should_not_run)

    with pytest.raises(SystemExit):
        live_real_trade.main([
            "--symbol",
            "BTC/USDT:USDT",
            "--once",
            "--confirm",
            "WRONG",
        ])


def test_cli_builds_real_small_cap_config_with_updated_caps():
    args = live_real_trade.parse_args([
        "--symbol",
        "BTC/USDT:USDT",
        "--once",
        "--confirm",
        live_real_trade.LIVE_REAL_CONFIRM_TEXT,
        "--max-notional",
        "999",
        "--max-loss",
        "999",
        "--leverage",
        "99",
    ])

    cfg = live_real_trade.build_live_real_config(args, {"testnet": True})

    assert cfg["live_activation_stage"] == "LIVE_REAL_SMALL_CAP"
    assert cfg["testnet"] is False
    assert cfg["live_trading"] is True
    assert cfg["real_order_enabled"] is True
    assert cfg["max_leverage"] == 5
    assert cfg["leverage"] == 5
    assert cfg["max_real_position_notional_usdt"] == pytest.approx(5.58)
    assert cfg["max_real_loss_per_trade_usdt"] == pytest.approx(0.31)
    assert cfg["max_daily_real_loss_usdt"] == pytest.approx(2.232)
    assert cfg["max_weekly_real_loss_usdt"] == pytest.approx(11.16)
    assert cfg["max_real_position_notional_pct_of_equity"] == pytest.approx(0.09)


def test_cli_main_runs_once_when_safety_tokens_are_present(monkeypatch, capsys):
    async def fake_run_once(args):
        return {"status": "NO_TRADE", "symbol": args.symbol}

    monkeypatch.setattr(live_real_trade, "run_once", fake_run_once)

    result = live_real_trade.main([
        "--symbol",
        "BTC/USDT:USDT",
        "--once",
        "--confirm",
        live_real_trade.LIVE_REAL_CONFIRM_TEXT,
    ])

    out = capsys.readouterr().out
    assert result["status"] == "NO_TRADE"
    assert '"status": "NO_TRADE"' in out


def test_cli_absolute_caps_reduce_effective_limits():
    args = live_real_trade.parse_args([
        "--symbol",
        "BTC/USDT:USDT",
        "--once",
        "--confirm",
        live_real_trade.LIVE_REAL_CONFIRM_TEXT,
        "--max-notional",
        "4",
        "--max-loss",
        "0.2",
    ])

    cfg = live_real_trade.build_live_real_config(args)

    assert cfg["live_real_absolute_max_notional_usdt"] == pytest.approx(4.0)
    assert cfg["live_real_absolute_max_loss_usdt"] == pytest.approx(0.2)
    assert cfg["max_real_position_notional_usdt"] == pytest.approx(4.0)
    assert cfg["max_real_loss_per_trade_usdt"] == pytest.approx(0.2)
    assert cfg["live_real_effective_limits"]["absolute_notional_cap_applied"] is True
    assert cfg["live_real_effective_limits"]["absolute_loss_cap_applied"] is True


@pytest.mark.parametrize(
    ("flag", "value"),
    (("--max-notional", "0"), ("--max-notional", "-1"), ("--max-loss", "0"), ("--max-loss", "-1")),
)
def test_cli_rejects_nonpositive_absolute_caps(flag, value):
    argv = [
        "--symbol",
        "BTC/USDT:USDT",
        "--once",
        "--confirm",
        live_real_trade.LIVE_REAL_CONFIRM_TEXT,
        flag,
        value,
    ]
    args = live_real_trade.parse_args(argv)
    with pytest.raises(ValueError):
        live_real_trade.build_live_real_config(args)
