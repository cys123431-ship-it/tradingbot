from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"{label}: target block not found in {path}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


configuration = Path("bot_runtime/configuration.py")
replace_once(
    configuration,
    "from __future__ import annotations\n\n",
    "from __future__ import annotations\n\nfrom datetime import datetime, timezone\n\n",
    "configuration datetime import",
)
replace_once(
    configuration,
    """        extension_date = str(
            automatic_controls_cfg.get('daily_trade_limit_extension_utc_date', '') or ''
        ).strip()
        if automatic_controls_cfg.get('daily_trade_limit_extension_utc_date') != extension_date:
            automatic_controls_cfg['daily_trade_limit_extension_utc_date'] = extension_date
            changed = True

        tp_sl_master = bool(common_cfg.get('tp_sl_enabled', True))
""",
    """        extension_date = str(
            automatic_controls_cfg.get('daily_trade_limit_extension_utc_date', '') or ''
        ).strip()
        if automatic_controls_cfg.get('daily_trade_limit_extension_utc_date') != extension_date:
            automatic_controls_cfg['daily_trade_limit_extension_utc_date'] = extension_date
            changed = True

        # Migrate the legacy `/utbreak dailytrades 10` setting into today's
        # one-time extension. The raw strategy-profile field is reset to the
        # fixed base value so it cannot silently grant 10 entries every day.
        strategy_params_cfg = signal_cfg.setdefault('strategy_params', {})
        utbreak_cfg = (
            strategy_params_cfg.setdefault('UTBotFilteredBreakoutV1', {})
            if isinstance(strategy_params_cfg, dict)
            else {}
        )
        if isinstance(utbreak_cfg, dict):
            try:
                legacy_daily_limit = int(float(utbreak_cfg.get('max_daily_trades', 5) or 5))
            except (TypeError, ValueError):
                legacy_daily_limit = 5
            if legacy_daily_limit >= 10:
                today_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d')
                if extension_date != today_utc:
                    automatic_controls_cfg['daily_trade_limit_extension_utc_date'] = today_utc
                    extension_date = today_utc
                    changed = True
            if utbreak_cfg.get('max_daily_trades') != 5:
                utbreak_cfg['max_daily_trades'] = 5
                changed = True

        tp_sl_master = bool(common_cfg.get('tp_sl_enabled', True))
""",
    "legacy daily-limit migration",
)

runtime_profile = Path("bot_runtime/runtime_profile.py")
replace_once(
    runtime_profile,
    """def build_utbreakout_effective_status_contract(cfg, daily_entries=None):
    \"\"\"Build the authoritative profile lines shown in every UTBreak status.\"\"\"
    effective = apply_profit_opportunity_effective_overrides(dict(cfg or {}))
""",
    """def build_utbreakout_effective_status_contract(cfg, daily_entries=None):
    \"\"\"Build the authoritative profile lines shown in every UTBreak status.\"\"\"
    source_cfg = dict(cfg or {})
    try:
        requested_daily_limit = int(float(source_cfg.get('max_daily_trades', 5) or 5))
    except (TypeError, ValueError):
        requested_daily_limit = 5
    effective = apply_profit_opportunity_effective_overrides(source_cfg)
    if requested_daily_limit in {5, 10}:
        effective['max_daily_trades'] = requested_daily_limit
""",
    "status contract dynamic limit",
)
replace_once(
    runtime_profile,
    """def enforce_utbreakout_effective_status_contract(text, cfg, daily_entries=None):
    \"\"\"Replace stale profile summary lines at the final Telegram render boundary.\"\"\"
    effective = apply_profit_opportunity_effective_overrides(dict(cfg or {}))
""",
    """def enforce_utbreakout_effective_status_contract(text, cfg, daily_entries=None):
    \"\"\"Replace stale profile summary lines at the final Telegram render boundary.\"\"\"
    source_cfg = dict(cfg or {})
    try:
        requested_daily_limit = int(float(source_cfg.get('max_daily_trades', 5) or 5))
    except (TypeError, ValueError):
        requested_daily_limit = 5
    effective = apply_profit_opportunity_effective_overrides(source_cfg)
    if requested_daily_limit in {5, 10}:
        effective['max_daily_trades'] = requested_daily_limit
""",
    "final Telegram contract dynamic limit",
)

telegram = Path("bot_runtime/controller_telegram_setup.py")
replace_once(
    telegram,
    """            daily_trade_limit = int(float(cfg.get('max_daily_trades', 5) if cfg.get('max_daily_trades', 5) is not None else 5))
            daily_trade_limit_text = \"OFF\" if daily_trade_limit <= 0 else f\"{daily_trade_limit}회\"
""",
    """            if hasattr(self, 'get_effective_automatic_daily_trade_limit'):
                daily_trade_limit = int(self.get_effective_automatic_daily_trade_limit())
            else:
                daily_trade_limit = int(float(
                    cfg.get('max_daily_trades', 5)
                    if cfg.get('max_daily_trades', 5) is not None
                    else 5
                ))
            daily_trade_limit_text = \"OFF\" if daily_trade_limit <= 0 else f\"{daily_trade_limit}회\"
""",
    "UTBreak menu effective limit display",
)
replace_once(
    telegram,
    """            elif action in {'dailytrades', 'daily_trades', 'daytrades', 'day_trades', 'maxtrades', 'max_trades'}:
                try:
                    raw_value = _parse_float_arg('일일 거래횟수 제한', minimum=1.0, maximum=1000.0)
                    if raw_value != int(raw_value):
                        raise ValueError(\"일일 거래횟수 제한은 정수로 입력하세요.\")
                    value = int(raw_value)
                except ValueError as e:
                    await u.message.reply_text(f\"❌ {e}\\n예: `/utbreak dailytrades 3`\", parse_mode=ParseMode.MARKDOWN)
                    return
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'max_daily_trades'],
                    value
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await u.message.reply_text(f\"✅ 일일 거래횟수 제한: {value}회\")
""",
    """            elif action in {'dailytrades', 'daily_trades', 'daytrades', 'day_trades', 'maxtrades', 'max_trades'}:
                try:
                    raw_value = _parse_float_arg('자동매매 일일 거래횟수', minimum=5.0, maximum=10.0)
                    if raw_value != int(raw_value) or int(raw_value) not in {5, 10}:
                        raise ValueError(\"자동매매 일일 한도는 기본 5회 또는 당일 확장 10회만 지원합니다.\")
                    value = int(raw_value)
                except ValueError as e:
                    await u.message.reply_text(
                        f\"❌ {e}\\n예: `/utbreak dailytrades 10` 또는 `/autotrade`\",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
                if value == 10:
                    changed, status = await self.extend_automatic_daily_trade_limit_for_today()
                    notice = (
                        \"✅ 오늘 자동매매 한도를 10회로 확장했습니다.\"
                        if changed
                        else \"ℹ️ 오늘 자동매매 한도는 이미 10회로 확장되어 있습니다.\"
                    )
                else:
                    status = self.get_automatic_trading_control_status()
                    notice = (
                        \"ℹ️ 기본 한도는 매일 5회입니다. \"
                        \"이미 적용된 당일 10회 확장은 UTC 날짜가 바뀔 때까지 유지됩니다.\"
                    )
                await u.message.reply_text(
                    f\"{notice}\\n현재 유효 한도: {status['effective_limit']}회 / \"
                    f\"오늘 진입: {status['entries']}회 / 남음: {status['remaining']}회\"
                )
""",
    "legacy dailytrades command routing",
)
replace_once(
    telegram,
    """            if action == 'dailytrades':
                try:
                    numeric_value = float(str(value or '').replace(',', '').strip())
                except (TypeError, ValueError):
                    await _edit_utbreakout_menu(query, \"❌ 버튼 값 처리 실패\")
                    return
                if numeric_value < 1 or numeric_value != int(numeric_value):
                    await _edit_utbreakout_menu(query, \"❌ 일일 거래횟수 제한은 1 이상의 정수여야 합니다.\")
                    return
                trade_limit = int(numeric_value)
                await self.cfg.update_value(
                    ['signal_engine', 'strategy_params', 'UTBotFilteredBreakoutV1', 'max_daily_trades'],
                    trade_limit
                )
                self._reset_signal_engine_runtime_state(reset_stateful_strategy=True)
                await _edit_utbreakout_menu(query, f\"✅ 일일 거래횟수 제한: {trade_limit}회\")
                return
""",
    """            if action == 'dailytrades':
                try:
                    numeric_value = float(str(value or '').replace(',', '').strip())
                except (TypeError, ValueError):
                    await _edit_utbreakout_menu(query, \"❌ 버튼 값 처리 실패\")
                    return
                if numeric_value != int(numeric_value) or int(numeric_value) not in {5, 10}:
                    await _edit_utbreakout_menu(
                        query,
                        \"❌ 자동매매 일일 한도는 기본 5회 또는 당일 확장 10회만 지원합니다.\",
                    )
                    return
                trade_limit = int(numeric_value)
                if trade_limit == 10:
                    changed, status = await self.extend_automatic_daily_trade_limit_for_today()
                    notice = (
                        \"✅ 오늘 자동매매 한도를 10회로 확장했습니다.\"
                        if changed
                        else \"ℹ️ 오늘 자동매매 한도는 이미 10회로 확장되어 있습니다.\"
                    )
                else:
                    status = self.get_automatic_trading_control_status()
                    notice = (
                        \"ℹ️ 기본 한도는 매일 5회이며, 적용된 당일 10회 확장은 \"
                        \"UTC 날짜가 바뀔 때까지 유지됩니다.\"
                    )
                await _edit_utbreakout_menu(
                    query,
                    f\"{notice} 현재 유효 한도 {status['effective_limit']}회 / \"
                    f\"오늘 {status['entries']}회 / 남음 {status['remaining']}회\",
                )
                return
""",
    "legacy dailytrades callback routing",
)

tests = Path("tests/test_automatic_trading_controls.py")
replace_once(
    tests,
    "import asyncio\nfrom datetime import datetime, timezone\n",
    "import asyncio\nimport json\nfrom datetime import datetime, timezone\n",
    "test json import",
)
tests.write_text(
    tests.read_text(encoding="utf-8")
    + """


def test_utbreak_status_contract_keeps_todays_extended_limit():
    cfg = emas.apply_profit_opportunity_effective_overrides({})
    cfg[\"max_daily_trades\"] = 10

    lines = emas.build_utbreakout_effective_status_contract(cfg, daily_entries=6)
    assert \"일일 리스크: trades 6/10\" in lines

    rendered = emas.enforce_utbreakout_effective_status_contract(
        \"일일 리스크: trades 6/5\",
        cfg,
        daily_entries=6,
    )
    assert \"일일 리스크: trades 6/10\" in rendered
    assert \"trades 6/5\" not in rendered


def test_legacy_persisted_ten_is_migrated_to_today_extension(tmp_path):
    path = tmp_path / \"config.json\"
    path.write_text(
        json.dumps(
            {
                \"signal_engine\": {
                    \"strategy_params\": {
                        \"UTBotFilteredBreakoutV1\": {\"max_daily_trades\": 10}
                    }
                }
            }
        ),
        encoding=\"utf-8\",
    )

    cfg = emas.TradingConfig(str(path))
    signal_cfg = cfg.get(\"signal_engine\", {})
    controls = signal_cfg[\"automatic_trading_controls\"]
    raw_ut = signal_cfg[\"strategy_params\"][\"UTBotFilteredBreakoutV1\"]

    assert controls[\"daily_trade_limit_extension_utc_date\"] == datetime.now(
        timezone.utc
    ).strftime(\"%Y-%m-%d\")
    assert raw_ut[\"max_daily_trades\"] == 5
""",
    encoding="utf-8",
)
