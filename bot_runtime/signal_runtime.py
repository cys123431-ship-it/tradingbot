"""Runtime state, reconciliation, cooldown, and lockout management."""

from __future__ import annotations


class SignalRuntimeMixin:
    async def _shutdown_crypto_safety_runtime(self):
        startup_task = getattr(self, 'crypto_safety_startup_task', None)
        self.crypto_safety_startup_task = None
        if startup_task is not None and not startup_task.done():
            startup_task.cancel()
            try:
                await startup_task
            except asyncio.CancelledError:
                pass

        stream = getattr(self, 'user_data_stream', None)
        self.user_data_stream = None
        if stream is not None:
            await stream.stop()

    def _set_crypto_entry_lock(self, reason):
        self.crypto_entry_lock_reason = str(reason or '') or None
        store = getattr(self, 'trading_state_store', None)
        if store is not None:
            store.set_runtime_state('entry_lock_reason', self.crypto_entry_lock_reason)

    async def _reconcile_crypto_exchange_state(
        self,
        *,
        user_stream_ready=False,
        require_user_stream=False,
    ):
        _ensure_trading_safety_runtime(self)
        common = self.get_runtime_common_settings() if hasattr(self, 'get_runtime_common_settings') else {}
        result = await reconcile_exchange_state(
            self.exchange,
            self.trading_state_store,
            single_position=bool(common.get('single_position_mode', True)),
            liquidation_config=common,
            user_stream_ready=bool(user_stream_ready),
            require_user_stream=bool(require_user_stream),
        )
        self.last_crypto_reconciliation = result.__dict__
        critical_pause = load_critical_pause_state()
        if not result.safe_to_trade:
            self._set_crypto_entry_lock(
                'RECONCILIATION_REQUIRED: ' + ', '.join(result.issues[:5])
            )
        elif critical_pause:
            self._set_crypto_entry_lock(
                'CRITICAL_PAUSE:'
                + str(critical_pause.get('reason') or 'manual reconciliation required')
            )
        else:
            self._set_crypto_entry_lock(None)
        logger.warning(
            "CRYPTO_RECONCILIATION safe=%s positions=%d open_orders=%d "
            "issues=%s unresolved=%s",
            result.safe_to_trade,
            len(result.positions),
            len(result.open_orders),
            result.issues,
            result.unresolved_records,
        )
        return result

    async def _startup_crypto_safety_reconciliation(self):
        self._set_crypto_entry_lock('RECONCILIATION_REQUIRED')
        try:
            common = self.get_runtime_common_settings() if hasattr(self, 'get_runtime_common_settings') else {}
            stream_required = bool(
                common.get('user_data_stream_enabled', True)
                and not self.is_upbit_mode()
            )
            await self._reconcile_crypto_exchange_state(
                user_stream_ready=False,
                require_user_stream=False,
            )
            if stream_required:
                self._set_crypto_entry_lock('USER_STREAM_CONNECTING')
            await self._recover_open_utbreakout_positions_on_start()
            final = await self._reconcile_crypto_exchange_state(
                user_stream_ready=False,
                require_user_stream=False,
            )
            if stream_required:
                self._set_crypto_entry_lock('USER_STREAM_CONNECTING')
            if not final.safe_to_trade:
                if getattr(self, 'ctrl', None) is not None:
                    reconciliation_details = list(final.issues)
                    reconciliation_details.extend(final.unresolved_records)
                    await self.ctrl.notify(
                        "CRITICAL: exchange reconciliation required. New entries are locked. "
                        + '; '.join(reconciliation_details[:5])
                    )
                return
            if stream_required:
                self._set_crypto_entry_lock('USER_STREAM_CONNECTING')
                async def _stream_reconcile():
                    refreshed = await self._reconcile_crypto_exchange_state(
                        user_stream_ready=True,
                        require_user_stream=True,
                    )
                    return bool(refreshed.safe_to_trade)

                controller_mode = None
                if getattr(self, 'ctrl', None) is not None and hasattr(self.ctrl, 'get_exchange_mode'):
                    controller_mode = self.ctrl.get_exchange_mode()
                stream_testnet = (
                    str(controller_mode or '').lower() == BINANCE_TESTNET
                    if controller_mode is not None
                    else bool(common.get('testnet', False))
                )
                self.user_data_stream = BinanceUserDataStream(
                    self.exchange,
                    self.trading_state_store,
                    testnet=stream_testnet,
                    reconcile_callback=_stream_reconcile,
                    lock_callback=self._set_crypto_entry_lock,
                )
                self.user_data_stream.start()
        except Exception as exc:
            self._set_crypto_entry_lock(f'RECONCILIATION_REQUIRED: {type(exc).__name__}')
            logger.exception("Crypto startup safety reconciliation failed")
            if getattr(self, 'ctrl', None) is not None:
                try:
                    await self.ctrl.notify(
                        f"CRITICAL: startup exchange reconciliation failed ({type(exc).__name__}). "
                        "New entries are locked."
                    )
                except Exception:
                    logger.exception("Startup reconciliation Telegram alert failed")

    def reset_entry_runtime_state(self):
        self.last_candle_time = {}
        self.last_candle_success = {}
        self.last_processed_candle_ts = {}
        self.utbreakout_entry_trace = {}
        self.utbreakout_last_ready_ts = {}
        self.utbreakout_last_ready_side = {}
        self.utbreakout_last_order_attempt_ts = {}
        self.utbreakout_last_watchdog_report_ts = {}
        self.utbreakout_auto_entry_bridge_last_attempt_ts = {}
        self.utbreakout_last_status_symbol = None
        self.utbreakout_last_ready_symbol = None
        self.current_utbreakout_candidate_symbol = None
        self.utbreakout_status_symbol_source = None
        self.utbreakout_status_symbol_detail = None
        self.utbot_filtered_breakout_entry_plans = {}
        self.last_utbot_filtered_breakout_status = {}
        self.last_entry_reason = {}

    def reset_exit_runtime_state(self):
        self.last_processed_exit_candle_ts = {}

    def reset_stateful_strategy_runtime_state(self):
        self.last_state_sync_candle_ts = {}
        self.last_stateful_retry_ts = {}
        self.last_utbreakout_no_position_retry_ts = {}
        self.last_stateful_diag = {}
        self.last_stateful_diag_notice = {}
        self.last_entry_filter_status = {}
        self.last_exit_filter_status = {}
        self.last_utbot_filter_pack_status = {}
        self.last_utbot_rsi_momentum_filter_status = {}
        self.last_utsmc_candidate_filter_status = {}
        self.last_utbot_filtered_breakout_status = {}
        self.utbot_filtered_breakout_entry_plans = {}
        self.utbreakout_trailing_states = {}
        self.aggressive_growth_positions = {}
        self.aggressive_growth_high_watermark = 0.0
        self.utbot_filtered_breakout_failures = {}
        self.utbreakout_futures_context_cache = {}
        self.utbreakout_orderflow_snapshots = {}
        self.utbreakout_market_regime_cache = {}
        self.utbreakout_shadow_pending = {}
        self.utbreakout_shadow_resolved_keys = set()
        self.utbreakout_shadow_stats_cache = {}
        self.utbreakout_runner_stats_cache = {}
        self.utbreakout_profit_alpha_meta_stats = {}
        self.relative_strength_pullback_states = {}
        self.relative_strength_pullback_last_decisions = {}
        self.relative_strength_pullback_eval_cache = {}
        self.dual_alpha_last_status = {}
        self.qh_flow_signal_cache = {}
        self.qh_flow_last_status = {}
        self.l2_gate_cache = {}
        self.l2_gate_history = {}
        self.crowding_unwind_last_status = {}
        self.liquidation_exhaustion_reversal_last_status = {}
        self.strategy_allocator_last_status = {}
        self.strategy_allocator_cache = {}
        self.triple_alpha_last_status = {}
        self.quad_alpha_last_status = {}
        self.last_live_entry_snapshot = {}
        self.utbreakout_adaptive_tf_state = {}
        self.utbreakout_adaptive_last_decision_ts = {}
        self.utbreakout_last_selected_set_ids = {}
        self.last_protection_order_status = {}
        self.last_protection_alert_ts = {}
        self.protection_missing_candidates = {}
        self.last_orphan_protection_sweep_ts = 0.0
        self.orphan_protection_candidates = {}
        self.flat_protected_state_candidates = {}
        self.coin_selector_last_result = {}
        self.coin_selector_symbol_scores = {}
        self.coin_selector_last_run_ts = 0.0
        self.coin_selector_candidate_cooldowns = {}
        self.coin_selector_analysis_cursor = 0
        self.coin_selector_strategy_cursor = 0
        self.coin_selector_rate_limit_backoff_until = 0.0
        self.utbreakout_daily_sl_symbol_lockouts = {}
        self.utbreakout_recent_loss_symbol_cooldowns = {}
        self._load_utbreakout_daily_sl_lockouts()
        self._load_utbreakout_profit_alpha_meta_stats()
        self.micro_auto_last_plan = {}
        self.micro_auto_last_rejects = {}
        self.micro_auto_last_scan = {}
        self.last_entry_reason = {}
        self.pending_reentry = {}
        self.ut_hybrid_timing_latches = {}
        self.ut_hybrid_timing_consumed_ts = {}
        self.utbb_special_long_state = {}
        self.utbb_special_short_state = {}
        self.ut_pending_entries = {}
        self.ut_strategy_signal_state = {}
        self.utsmc_pending_entries = {}
        self.utsmc_last_entry_signal_ts = {}
        self.utsmc_entry_invalidation = {}
        self.utsmc_fixed_exit_obs = {}

    def reset_signal_runtime_state(self, *, reset_entry_cache=False, reset_exit_cache=False, reset_stateful_strategy=False):
        if reset_entry_cache:
            self.reset_entry_runtime_state()
        if reset_exit_cache:
            self.reset_exit_runtime_state()
        if reset_stateful_strategy:
            self.reset_stateful_strategy_runtime_state()

    def _ensure_runtime_state_container(self, attr_name, default_factory=dict):
        value = getattr(self, attr_name, None)
        if not isinstance(value, dict):
            logger.error(f"State corrupted: {attr_name} is {type(value)}, resetting.")
            value = default_factory()
            setattr(self, attr_name, value)
        return value

    def _ensure_runtime_state_containers(self, attr_names):
        for attr_name in attr_names:
            self._ensure_runtime_state_container(attr_name)

    def _utbreakout_today_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _normalize_market_symbol(self, symbol: str) -> str:
        try:
            return self._canonical_futures_symbol(symbol)
        except Exception:
            return str(symbol or "").strip().upper()

    def _utbreakout_daily_sl_lockouts_path(self) -> str:
        runtime_dir = (
            getattr(self, 'runtime_dir', None)
            or os.environ.get('TRADINGBOT_RUNTIME_DIR')
            or "runtime"
        )
        return os.path.join(str(runtime_dir), "utbreakout_daily_sl_lockouts.json")

    def _utbreakout_profit_alpha_meta_path(self) -> str:
        runtime_dir = (
            getattr(self, 'runtime_dir', None)
            or os.environ.get('TRADINGBOT_RUNTIME_DIR')
            or "runtime"
        )
        return os.path.join(str(runtime_dir), "utbreakout_profit_alpha_meta.json")

    def _save_utbreakout_profit_alpha_meta_stats(self):
        try:
            stats = getattr(self, 'utbreakout_profit_alpha_meta_stats', {})
            path = self._utbreakout_profit_alpha_meta_path()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            atomic_write_json(path, stats if isinstance(stats, dict) else {}, indent=4)
            logger.info(
                "Saved UTBreakout profit alpha meta stats: count=%s path=%s",
                len(stats) if isinstance(stats, dict) else 0,
                path,
            )
        except Exception as exc:
            logger.warning("Failed to save UTBreakout profit alpha meta stats: %s", exc)

    def _load_utbreakout_profit_alpha_meta_stats(self):
        path = self._utbreakout_profit_alpha_meta_path()
        if not os.path.exists(path):
            self.utbreakout_profit_alpha_meta_stats = {}
            logger.info("No UTBreakout profit alpha meta file found: %s", path)
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                self.utbreakout_profit_alpha_meta_stats = {}
                logger.warning("Ignored malformed UTBreakout profit alpha meta file: %s", path)
                return
            normalized = {}
            for key, record in data.items():
                if not isinstance(record, dict):
                    continue
                try:
                    count = int(float(record.get('sample_count', record.get('trades', 0)) or 0))
                except (TypeError, ValueError):
                    count = 0
                if count <= 0:
                    continue
                normalized[str(key)] = {
                    'sample_count': count,
                    'win_count': int(float(record.get('win_count', 0) or 0)),
                    'loss_count': int(float(record.get('loss_count', 0) or 0)),
                    'avg_pnl_r': float(record.get('avg_pnl_r', 0.0) or 0.0),
                    'expectancy_r': float(record.get('expectancy_r', record.get('avg_pnl_r', 0.0)) or 0.0),
                    'last_pnl_r': float(record.get('last_pnl_r', 0.0) or 0.0),
                    'updated_at': record.get('updated_at'),
                }
            self.utbreakout_profit_alpha_meta_stats = normalized
            logger.info(
                "Loaded UTBreakout profit alpha meta stats: count=%s path=%s",
                len(normalized),
                path,
            )
        except Exception as exc:
            logger.warning("Failed to load UTBreakout profit alpha meta stats: %s", exc)
            self.utbreakout_profit_alpha_meta_stats = {}

    def _profit_alpha_meta_key(self, side, engine):
        side_key = str(side or '').lower()
        engine_key = str(engine or 'UNKNOWN').upper()
        if side_key not in {'long', 'short'}:
            side_key = 'unknown'
        return f"{side_key}:{engine_key}"

    def _profit_alpha_meta_snapshot(self, side=None, engine=None):
        stats = getattr(self, 'utbreakout_profit_alpha_meta_stats', {})
        if not isinstance(stats, dict):
            stats = {}
            self.utbreakout_profit_alpha_meta_stats = stats
        if side and engine:
            key = self._profit_alpha_meta_key(side, engine)
            return {key: dict(stats.get(key, {}))}
        return dict(stats)

    def _record_profit_alpha_meta_outcome(self, symbol, state, outcome):
        if not isinstance(state, dict) or not isinstance(outcome, dict):
            return
        side = str(state.get('side') or '').lower()
        engine = str(state.get('profit_alpha_engine') or '').upper()
        if side not in {'long', 'short'} or not engine or engine in {'DISABLED', 'NONE'}:
            return
        pnl_r = _safe_float_or_none(outcome.get('pnl_r'))
        if pnl_r is None:
            return
        stats = self._ensure_runtime_state_container('utbreakout_profit_alpha_meta_stats')
        exit_policy = str(state.get('profit_alpha_exit_policy') or '').upper()
        keys = [self._profit_alpha_meta_key(side, engine)]
        if exit_policy:
            keys.append(f"{side}:{engine}:{exit_policy}")
            keys.append(f"exit:{exit_policy}")
        first_count = 0
        first_avg = 0.0
        for key in keys:
            record = stats.get(key) if isinstance(stats.get(key), dict) else {}
            count = int(record.get('sample_count', 0) or 0) + 1
            prev_avg = float(record.get('avg_pnl_r', 0.0) or 0.0)
            avg_pnl_r = prev_avg + (float(pnl_r) - prev_avg) / max(count, 1)
            win_count = int(record.get('win_count', 0) or 0) + (1 if pnl_r > 0 else 0)
            loss_count = int(record.get('loss_count', 0) or 0) + (1 if pnl_r < 0 else 0)
            stats[key] = {
                'sample_count': count,
                'win_count': win_count,
                'loss_count': loss_count,
                'avg_pnl_r': avg_pnl_r,
                'expectancy_r': avg_pnl_r,
                'last_pnl_r': float(pnl_r),
                'symbol': self._normalize_market_symbol(symbol),
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }
            if key == keys[0]:
                first_count = count
                first_avg = avg_pnl_r
        self._save_utbreakout_profit_alpha_meta_stats()
        try:
            self._utbreakout_trace_event(
                symbol,
                'PROFIT_ALPHA_META',
                'UPDATED',
                side=side,
                engine=engine,
                exit_policy=exit_policy or '',
                sample_count=first_count,
                expectancy_r=round(first_avg, 4),
                last_pnl_r=round(float(pnl_r), 4),
            )
        except Exception:
            logger.debug("UTBreakout profit alpha meta trace skipped", exc_info=True)

    def _record_utbreakout_daily_sl_lockout(
        self,
        symbol: str,
        *,
        side: str | None = None,
        reason: str,
        detail: str = "",
    ) -> None:
        normalized_symbol = self._normalize_market_symbol(symbol)
        if not normalized_symbol:
            return

        lockouts = self._ensure_runtime_state_container('utbreakout_daily_sl_symbol_lockouts')
        today_key = self._utbreakout_today_key()
        now_ms = int(time.time() * 1000)

        lockouts[normalized_symbol] = {
            "date": today_key,
            "reason": reason,
            "side": side,
            "ts": now_ms,
            "detail": detail[:240],
        }

        self._save_utbreakout_daily_sl_lockouts()

        self._utbreakout_trace_event(
            normalized_symbol,
            "DAILY_SL_LOCKOUT",
            "RECORDED",
            side=side,
            reason=reason,
            detail=detail[:240],
        )

        try:
            tg_msg = (
                f"🛑 UTBreakout daily symbol lockout\n"
                f"Symbol: {normalized_symbol}\n"
                f"Reason: {reason}\n"
                f"Action: same symbol re-entry blocked until tomorrow ({today_key})"
            )
            if hasattr(self, 'telegram') and self.telegram:
                self.telegram.send_message_to_all(tg_msg)
        except Exception as e:
            logger.warning(f"Failed to send lockout Telegram notification: {e}")

    def _is_utbreakout_daily_sl_locked(self, symbol: str) -> tuple[bool, str]:
        normalized_symbol = self._normalize_market_symbol(symbol)
        if not normalized_symbol:
            return False, ""

        lockouts = self._ensure_runtime_state_container('utbreakout_daily_sl_symbol_lockouts')
        record = lockouts.get(normalized_symbol)
        if not record:
            return False, ""

        today_key = self._utbreakout_today_key()
        if record.get("date") != today_key:
            lockouts.pop(normalized_symbol, None)
            self._save_utbreakout_daily_sl_lockouts()
            return False, ""

        reason = record.get("reason", "UNKNOWN")
        return True, f"당일 SL lockout / protection lockout: {reason} today (daily SL lockout)"

    def _utbreakout_recent_loss_cooldown_config(self, cfg=None):
        if not isinstance(cfg, dict):
            try:
                cfg = self._get_utbot_filtered_breakout_config(
                    self.get_runtime_strategy_params()
                )
            except Exception:
                cfg = {}
        cfg = apply_profit_opportunity_effective_overrides(dict(cfg or {}))
        raw_seconds = cfg.get(
            'utbreakout_recent_loss_cooldown_seconds',
            UTBREAKOUT_RECENT_LOSS_COOLDOWN_SECONDS,
        )
        if raw_seconds in (None, ''):
            raw_seconds = UTBREAKOUT_RECENT_LOSS_COOLDOWN_SECONDS
        try:
            seconds = float(raw_seconds)
        except (TypeError, ValueError):
            seconds = UTBREAKOUT_RECENT_LOSS_COOLDOWN_SECONDS
        raw_min_loss = cfg.get(
            'utbreakout_recent_loss_cooldown_min_loss_usdt',
            UTBREAKOUT_RECENT_LOSS_COOLDOWN_MIN_LOSS_USDT,
        )
        if raw_min_loss in (None, ''):
            raw_min_loss = UTBREAKOUT_RECENT_LOSS_COOLDOWN_MIN_LOSS_USDT
        try:
            min_loss = float(raw_min_loss)
        except (TypeError, ValueError):
            min_loss = UTBREAKOUT_RECENT_LOSS_COOLDOWN_MIN_LOSS_USDT
        if (
            seconds == UTBREAKOUT_RECENT_LOSS_LEGACY_COOLDOWN_SECONDS
            and min_loss == UTBREAKOUT_RECENT_LOSS_LEGACY_MIN_LOSS_USDT
        ):
            seconds = UTBREAKOUT_RECENT_LOSS_COOLDOWN_SECONDS
            min_loss = UTBREAKOUT_RECENT_LOSS_COOLDOWN_MIN_LOSS_USDT
        return {
            'enabled': bool(cfg.get('utbreakout_recent_loss_cooldown_enabled', True)),
            'seconds': max(0.0, seconds),
            'min_loss_usdt': max(0.0, min_loss),
        }

    def _record_utbreakout_recent_loss_cooldown(
        self,
        symbol: str,
        *,
        side: str | None = None,
        pnl_usdt: float | None = None,
        reason: str = "RECENT_LOSS",
        cfg: dict | None = None,
    ) -> None:
        cd_cfg = self._utbreakout_recent_loss_cooldown_config(cfg)
        if not cd_cfg.get('enabled') or cd_cfg.get('seconds', 0.0) <= 0:
            return
        pnl_value = _safe_float_or_none(pnl_usdt)
        min_loss = float(cd_cfg.get('min_loss_usdt', 0.0) or 0.0)
        if pnl_value is not None and pnl_value >= -min_loss:
            return
        normalized_symbol = self._normalize_market_symbol(symbol)
        if not normalized_symbol:
            return
        cooldowns = self._ensure_runtime_state_container(
            'utbreakout_recent_loss_symbol_cooldowns'
        )
        now_ts = time.time()
        until_ts = now_ts + float(cd_cfg.get('seconds', 0.0) or 0.0)
        cooldowns[normalized_symbol] = {
            'until': until_ts,
            'side': side,
            'pnl_usdt': pnl_value,
            'reason': str(reason or 'RECENT_LOSS')[:120],
            'recorded_at': now_ts,
        }
        try:
            self._utbreakout_trace_event(
                normalized_symbol,
                'RECENT_LOSS_COOLDOWN',
                'RECORDED',
                side=side,
                pnl_usdt=pnl_value,
                reason=reason,
                cooldown_seconds=float(cd_cfg.get('seconds', 0.0) or 0.0),
            )
        except Exception:
            logger.debug("UTBreakout recent loss cooldown trace skipped", exc_info=True)

    def _recent_loss_cooldown_db_lookup(self, normalized_symbol: str, cd_cfg: dict):
        db = getattr(self, 'db', None)
        conn = getattr(db, 'conn', None)
        lock = getattr(db, 'lock', None)
        if conn is None or lock is None:
            return None
        aliases = {
            normalized_symbol,
            str(normalized_symbol or '').replace(':USDT', ''),
            str(normalized_symbol or '').replace('/', '').replace(':USDT', ''),
        }
        aliases = [item for item in aliases if item]
        if not aliases:
            return None
        placeholders = ",".join("?" for _ in aliases)
        query = (
            "SELECT side, pnl_usdt, exit_time, exit_reason "
            f"FROM trades WHERE symbol IN ({placeholders}) "
            "AND exit_time IS NOT NULL ORDER BY exit_time DESC, id DESC LIMIT 1"
        )
        try:
            with lock:
                row = conn.execute(query, tuple(aliases)).fetchone()
        except Exception:
            logger.debug("UTBreakout recent loss DB lookup failed", exc_info=True)
            return None
        if not row:
            return None
        side, pnl_usdt, exit_time, exit_reason = row
        pnl_value = _safe_float_or_none(pnl_usdt)
        min_loss = float(cd_cfg.get('min_loss_usdt', 0.0) or 0.0)
        if pnl_value is None or pnl_value >= -min_loss:
            return None
        try:
            exit_dt = datetime.fromisoformat(str(exit_time).replace('Z', '+00:00'))
            if exit_dt.tzinfo is None:
                exit_dt = exit_dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
        age_sec = (datetime.now(timezone.utc) - exit_dt.astimezone(timezone.utc)).total_seconds()
        if age_sec < 0 or age_sec > float(cd_cfg.get('seconds', 0.0) or 0.0):
            return None
        remaining_sec = max(0.0, float(cd_cfg.get('seconds', 0.0) or 0.0) - age_sec)
        return {
            'side': side,
            'pnl_usdt': pnl_value,
            'reason': str(exit_reason or 'RECENT_LOSS')[:120],
            'remaining_sec': remaining_sec,
        }

    def _is_utbreakout_recent_loss_cooldown_active(
        self,
        symbol: str,
        cfg: dict | None = None,
    ) -> tuple[bool, str]:
        cd_cfg = self._utbreakout_recent_loss_cooldown_config(cfg)
        if not cd_cfg.get('enabled') or cd_cfg.get('seconds', 0.0) <= 0:
            return False, ""
        normalized_symbol = self._normalize_market_symbol(symbol)
        if not normalized_symbol:
            return False, ""
        cooldowns = self._ensure_runtime_state_container(
            'utbreakout_recent_loss_symbol_cooldowns'
        )
        now_ts = time.time()
        record = cooldowns.get(normalized_symbol)
        if isinstance(record, dict):
            until_ts = _safe_float_or_none(record.get('until')) or 0.0
            if until_ts > now_ts:
                remaining_min = max(1, int((until_ts - now_ts + 59) // 60))
                reason = str(record.get('reason') or 'RECENT_LOSS')
                return True, (
                    f"recent loss cooldown: {reason}; "
                    f"{remaining_min}m remaining for {normalized_symbol}"
                )
            cooldowns.pop(normalized_symbol, None)
        db_record = self._recent_loss_cooldown_db_lookup(normalized_symbol, cd_cfg)
        if isinstance(db_record, dict):
            remaining_min = max(
                1,
                int((float(db_record.get('remaining_sec', 0.0) or 0.0) + 59) // 60),
            )
            reason = str(db_record.get('reason') or 'RECENT_LOSS')
            self._record_utbreakout_recent_loss_cooldown(
                normalized_symbol,
                side=db_record.get('side'),
                pnl_usdt=db_record.get('pnl_usdt'),
                reason=reason,
                cfg=cfg,
            )
            return True, (
                f"recent loss cooldown: {reason}; "
                f"{remaining_min}m remaining for {normalized_symbol}"
            )
        return False, ""

    def _save_utbreakout_daily_sl_lockouts(self):
        try:
            lockouts = getattr(self, 'utbreakout_daily_sl_symbol_lockouts', {})
            path = self._utbreakout_daily_sl_lockouts_path()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            atomic_write_json(path, lockouts, indent=4)
            logger.info(
                "Saved UTBreakout daily SL lockouts: count=%s path=%s",
                len(lockouts) if isinstance(lockouts, dict) else 0,
                path,
            )
        except Exception as e:
            logger.warning(f"Failed to save daily SL lockouts to disk: {e}")

    def _load_utbreakout_daily_sl_lockouts(self):
        default_lockouts = {}
        path = self._utbreakout_daily_sl_lockouts_path()
        if not os.path.exists(path):
            self.utbreakout_daily_sl_symbol_lockouts = default_lockouts
            logger.info("No UTBreakout daily SL lockouts file found: %s", path)
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                today_key = self._utbreakout_today_key()
                filtered = {}
                for symbol, record in data.items():
                    if not isinstance(record, dict) or record.get("date") != today_key:
                        continue
                    normalized_symbol = self._normalize_market_symbol(symbol)
                    if normalized_symbol:
                        filtered[normalized_symbol] = dict(record)
                self.utbreakout_daily_sl_symbol_lockouts = filtered
                if len(filtered) != len(data):
                    self._save_utbreakout_daily_sl_lockouts()
                logger.info(
                    "Loaded UTBreakout daily SL lockouts: count=%s path=%s",
                    len(filtered),
                    path,
                )
            else:
                self.utbreakout_daily_sl_symbol_lockouts = default_lockouts
                logger.warning(
                    "Ignored malformed UTBreakout daily SL lockouts file: %s",
                    path,
                )
        except Exception as e:
            logger.warning(f"Failed to load daily SL lockouts from disk: {e}")
            self.utbreakout_daily_sl_symbol_lockouts = default_lockouts

    async def _check_and_record_sl_lockout_async(self, symbol, state, exit_price):
        if not isinstance(state, dict):
            return
        is_sl_hit = False
        sl_id = state.get("sl_order_id")
        if sl_id:
            try:
                ord_info = await asyncio.to_thread(self.exchange.fetch_order, sl_id, symbol)
                if isinstance(ord_info, dict) and ord_info.get("status") == "closed":
                    is_sl_hit = True
            except Exception:
                pass
        if not is_sl_hit:
            last_stop = state.get("last_stop_price") or state.get("initial_stop_price")
            if last_stop and exit_price:
                diff_pct = abs(exit_price - last_stop) / last_stop
                if diff_pct <= 0.002:
                    is_sl_hit = True

        if is_sl_hit:
            self._record_utbreakout_daily_sl_lockout(
                symbol,
                side=state.get("side"),
                reason="STOP_LOSS_FILLED",
                detail=f"position closed by stop loss at {exit_price:.4f}" if exit_price else "position closed by stop loss",
            )

    async def _is_sl_lockout_active(self, symbol):
        locked, reason = self._is_utbreakout_daily_sl_locked(symbol)
        return locked

    def _coin_selector_candidate_key(self, symbol):
        return (
            str(symbol or '')
            .strip()
            .upper()
            .replace(':USDT', '')
            .replace('/', '')
            .replace('-', '')
            .replace('_', '')
        )

    def _coin_selector_cooldown_config(self, cfg=None):
        cfg = cfg if isinstance(cfg, dict) else default_coin_selector_config()

        def _bool_value(value, default=True):
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in {'1', 'true', 'yes', 'on', 'enable', 'enabled'}:
                return True
            if text in {'0', 'false', 'no', 'off', 'disable', 'disabled'}:
                return False
            return default

        def _positive_int(value, default):
            try:
                parsed = int(float(value))
            except (TypeError, ValueError):
                parsed = int(default)
            return max(1, parsed)

        def _positive_float(value, default):
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                parsed = float(default)
            return max(1.0, parsed)

        defaults = default_coin_selector_config()
        enabled = _bool_value(
            cfg.get('candidate_cooldown_enabled', defaults.get('candidate_cooldown_enabled', True)),
            bool(defaults.get('candidate_cooldown_enabled', True))
        )
        miss_limit = _positive_int(
            cfg.get('candidate_cooldown_misses', defaults.get('candidate_cooldown_misses', 3)),
            defaults.get('candidate_cooldown_misses', 3)
        )
        cooldown_seconds = _positive_float(
            cfg.get('candidate_cooldown_seconds', defaults.get('candidate_cooldown_seconds', 1800)),
            defaults.get('candidate_cooldown_seconds', 1800)
        )
        return enabled, miss_limit, cooldown_seconds

    def _coin_selector_cooldown_remaining(self, symbol, cfg=None, now=None):
        enabled, _, _ = self._coin_selector_cooldown_config(cfg)
        if not enabled:
            return 0.0, None
        key = self._coin_selector_candidate_key(symbol)
        if not key:
            return 0.0, None
        states = self._ensure_runtime_state_container('coin_selector_candidate_cooldowns')
        state = states.get(key)
        if not isinstance(state, dict):
            return 0.0, None
        now = time.time() if now is None else float(now)
        cooldown_until = float(state.get('cooldown_until', 0.0) or 0.0)
        if cooldown_until > now:
            return cooldown_until - now, state
        if cooldown_until:
            states.pop(key, None)
        return 0.0, None

    def _record_coin_selector_candidate_outcome(
        self,
        symbol,
        *,
        accepted=False,
        reason='',
        cfg=None,
        now=None,
        decision_key=None
    ):
        key = self._coin_selector_candidate_key(symbol)
        if not key:
            return None
        states = self._ensure_runtime_state_container('coin_selector_candidate_cooldowns')
        if accepted:
            states.pop(key, None)
            return None

        enabled, miss_limit, cooldown_seconds = self._coin_selector_cooldown_config(cfg)
        if not enabled:
            states.pop(key, None)
            return None

        now = time.time() if now is None else float(now)
        previous = states.get(key)
        state = dict(previous) if isinstance(previous, dict) else {}
        cooldown_until = float(state.get('cooldown_until', 0.0) or 0.0)
        if cooldown_until > now:
            return state
        if cooldown_until:
            state = {}

        normalized_decision_key = str(decision_key or '').strip()
        if normalized_decision_key and state.get('last_decision_key') == normalized_decision_key:
            return state

        miss_count = int(state.get('miss_count', 0) or 0) + 1
        state.update({
            'symbol': symbol,
            'miss_count': miss_count,
            'last_reason': str(reason or 'no entry').strip()[:240],
            'last_ts': now,
        })
        if normalized_decision_key:
            state['last_decision_key'] = normalized_decision_key

        if miss_count >= miss_limit:
            state['cooldown_until'] = now + cooldown_seconds
            state['cooldown_seconds'] = cooldown_seconds
            logger.info(
                f"CoinSelector candidate cooldown: {symbol} skipped for "
                f"{cooldown_seconds / 60.0:.1f}m after {miss_count} no-entry checks "
                f"({state['last_reason']})"
            )

        states[key] = state
        return state

    def _get_ut_entry_timing_mode(self):
        strategy_params = self.get_runtime_strategy_params()
        mode = str(strategy_params.get('ut_entry_timing_mode', 'next_candle') or 'next_candle').strip().lower()
        if mode not in {'next_candle', 'persistent'}:
            return 'next_candle'
        return mode

    def _get_ut_entry_timing_label(self, mode=None):
        parsed_mode = str(mode or self._get_ut_entry_timing_mode()).strip().lower()
        return '다음봉 진입' if parsed_mode == 'next_candle' else '신호유지 진입'
