"""Tests for bot_runtime.loop_watchdog (read-only diagnostics only)."""
from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from bot_runtime.loop_watchdog import LoopWatchdog


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = float(t)
        self.wall = 1_700_000_000.0

    def monotonic(self):
        return self.t

    def time(self):
        return self.wall


class FakeLogger:
    def __init__(self):
        self.warnings = []
        self.debugs = []

    def warning(self, msg, *args):
        self.warnings.append((msg, args))

    def debug(self, msg, *args):
        self.debugs.append((msg, args))


def _make(tmp_path, clock, logger):
    wd = LoopWatchdog(
        tmp_path / "loop_watchdog.json",
        logger=logger,
        threshold_sec=1.0,
        beat_interval=0.1,
        clock=clock.monotonic,
        wall_clock=clock.time,
    )
    # Simulate "started" state without launching threads/loop.
    wd._started_at = clock.time()
    wd._updated_at = clock.time()
    return wd


def test_blocked_warn_once_and_resume(tmp_path):
    clock = FakeClock()
    logger = FakeLogger()
    wd = _make(tmp_path, clock, logger)

    # Pretend beat was long ago.
    wd._last_beat = clock.monotonic() - 5.0
    wd._check_once()
    assert len([w for w in logger.warnings if w[0].startswith("EVENT_LOOP_BLOCKED")]) == 1

    # Second check while still blocked: no duplicate warning.
    clock.t += 0.5
    wd._check_once()
    assert len([w for w in logger.warnings if w[0].startswith("EVENT_LOOP_BLOCKED")]) == 1

    # Resume: set beat to now (simulates block ending).
    clock.t += 1.0
    wd._last_beat = clock.monotonic()
    wd._check_once()

    resumed = [w for w in logger.warnings if w[0].startswith("EVENT_LOOP_RESUMED")]
    assert len(resumed) == 1

    snap = wd.snapshot()
    assert snap["blocks_total"] == 1
    assert snap["max_block_sec"] > 1.0

    stats_path = tmp_path / "loop_watchdog.json"
    assert stats_path.exists()
    data = json.loads(stats_path.read_text(encoding="utf-8"))
    assert data["blocks_total"] == 1


def _blocking_test_function():
    # This name must appear in the captured stack.
    time.sleep(0.8)


def test_real_loop_blocking_detected(tmp_path):
    logger = FakeLogger()

    async def _scenario():
        wd = LoopWatchdog(
            tmp_path / "loop_watchdog.json",
            logger=logger,
            threshold_sec=0.3,
            beat_interval=0.05,
        )
        wd.start()
        try:
            _blocking_test_function()
            await asyncio.sleep(0.3)
        finally:
            wd.stop()

    asyncio.run(_scenario())

    blocked_msgs = [w for w in logger.warnings if w[0].startswith("EVENT_LOOP_BLOCKED")]
    assert blocked_msgs, f"no EVENT_LOOP_BLOCKED warning; got={logger.warnings}"
    # Args: (blocked, stack_text)
    stack_text = blocked_msgs[0][1][1]
    assert "_blocking_test_function" in stack_text
    assert "time.sleep" in stack_text


def test_system_status_is_safe(tmp_path):
    from scripts import bot_status_readonly as status

    info = status.system_status(tmp_path)
    assert isinstance(info, dict)
    for key in (
        "load_avg",
        "cpu_count",
        "mem_total_mb",
        "mem_available_mb",
        "bot_pid",
        "bot_rss_mb",
        "bot_cpu_percent",
        "bot_threads",
        "watchdog",
    ):
        assert key in info
    # On non-Linux or missing /proc, watchdog absence must be None.
    if not sys.platform.startswith("linux"):
        assert info["watchdog"] is None
