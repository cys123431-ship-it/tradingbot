"""Read-only event-loop watchdog. Detects synchronous blocking in the asyncio loop.

Never touches trading state; only observes and writes a stats JSON file.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

__all__ = ["LoopWatchdog"]


class LoopWatchdog:
    def __init__(
        self,
        stats_path,
        *,
        logger,
        threshold_sec=3.0,
        beat_interval=0.5,
        clock=time.monotonic,
        wall_clock=time.time,
        stack_limit=25,
    ):
        self._stats_path = Path(stats_path)
        self._logger = logger
        self._threshold_sec = float(threshold_sec)
        self._beat_interval = float(beat_interval)
        self._clock = clock
        self._wall_clock = wall_clock
        self._stack_limit = int(stack_limit)

        # Shared state (guarded by _lock where needed).
        self._lock = threading.Lock()
        self._last_beat = self._clock()
        self._loop_thread_id = None
        self._loop = None
        self._beat_task = None
        self._thread = None
        self._stop_event = threading.Event()

        # Block lifecycle tracking.
        self._reporting_block = False
        self._block_started_clock = None  # monotonic at block start (approx)

        # Stats.
        self._started_at = None
        self._updated_at = None
        self._blocks_total = 0
        self._max_block_sec = 0.0
        self._last_block_sec = None
        self._last_block_at = None
        self._last_block_stack_top = None
        self._recent_blocks = []

        # Stats file throttle.
        self._last_stats_write = 0.0

    # ------------------------------------------------------------------ start
    def start(self, loop=None):
        loop = loop or asyncio.get_running_loop()
        self._loop = loop
        self._loop_thread_id = threading.get_ident()
        now = self._wall_clock()
        if self._started_at is None:
            self._started_at = now
        self._updated_at = now
        # Initialize beat baseline right at start.
        self._last_beat = self._clock()
        self._stop_event.clear()
        self._reporting_block = False

        try:
            self._beat_task = loop.create_task(self._beat())
        except Exception as exc:  # pragma: no cover - defensive
            self._logger.debug("LoopWatchdog: failed to create beat task: %s", exc)
            self._beat_task = None

        self._thread = threading.Thread(
            target=self._run, daemon=True, name="loop-watchdog"
        )
        self._thread.start()

    # --------------------------------------------------------------- heartbeat
    async def _beat(self):
        interval = self._beat_interval
        try:
            while not self._stop_event.is_set():
                with self._lock:
                    self._last_beat = self._clock()
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self._logger.debug("LoopWatchdog: beat loop error: %s", exc)

    # -------------------------------------------------------------- watchdog
    def _run(self):
        try:
            while not self._stop_event.is_set():
                try:
                    self._check_once()
                except Exception as exc:  # never let the thread die
                    self._logger.debug("LoopWatchdog: check error: %s", exc)
                # Sleep in small chunks so stop() is responsive.
                self._stop_event.wait(self._beat_interval)
        except Exception as exc:  # pragma: no cover - defensive
            self._logger.debug("LoopWatchdog: thread crashed: %s", exc)

    def _check_once(self):
        """Single watchdog check. Safe to call from tests."""
        try:
            self._check_once_inner()
        except Exception as exc:  # pragma: no cover - defensive
            self._logger.debug("LoopWatchdog: check lost: %s", exc)

    def _check_once_inner(self):
        now_clock = self._clock()
        now_wall = self._wall_clock()
        with self._lock:
            last_beat = self._last_beat

        blocked = now_clock - last_beat

        if blocked > self._threshold_sec:
            if not self._reporting_block:
                # First detection of this block.
                self._reporting_block = True
                self._block_started_clock = last_beat
                stack_text, stack_lines = self._capture_stack()
                try:
                    self._logger.warning(
                        "EVENT_LOOP_BLOCKED %.1fs (still blocked); "
                        "loop thread stack:\n%s",
                        blocked,
                        stack_text,
                    )
                except Exception as exc:  # pragma: no cover
                    self._logger.debug("LoopWatchdog: warn failed: %s", exc)

                with self._lock:
                    self._blocks_total += 1
                    self._last_block_sec = float(blocked)
                    self._last_block_at = now_wall
                    if blocked > self._max_block_sec:
                        self._max_block_sec = float(blocked)
                    self._last_block_stack_top = stack_lines
                    top_line = stack_lines[-1] if stack_lines else ""
                    self._recent_blocks.append(
                        {"at": now_wall, "sec": float(blocked), "top": top_line}
                    )
                    if len(self._recent_blocks) > 20:
                        self._recent_blocks = self._recent_blocks[-20:]
                    self._updated_at = now_wall
            # else: already reporting this block; stay quiet.

            # Periodic stats flush even while blocked.
            if now_clock - self._last_stats_write >= 30.0:
                self._write_stats()
            return

        # Not currently blocked by watchdog definition.
        if self._reporting_block:
            self._reporting_block = False
            # Actual block duration: from last known block start to now.
            block_sec = now_clock - (self._block_started_clock or last_beat)
            if block_sec < 0:
                block_sec = 0.0
            with self._lock:
                if block_sec > self._max_block_sec:
                    self._max_block_sec = float(block_sec)
                self._last_block_sec = float(block_sec)
                self._last_block_at = now_wall
                self._updated_at = now_wall
                max_so_far = self._max_block_sec
            try:
                self._logger.warning(
                    "EVENT_LOOP_RESUMED after %.1fs", float(max_so_far)
                )
            except Exception as exc:  # pragma: no cover
                self._logger.debug("LoopWatchdog: resumed warn failed: %s", exc)
            self._block_started_clock = None
            self._write_stats()

    # ---------------------------------------------------------------- helpers
    def _capture_stack(self):
        thread_id = self._loop_thread_id
        if thread_id is None:
            return ("(no loop thread registered)", [])
        frame = sys._current_frames().get(thread_id)
        if frame is None:
            return ("(loop thread frame not found)", [])
        try:
            stack = traceback.format_stack(frame)
        except Exception as exc:  # pragma: no cover
            return (f"(stack capture failed: {exc})", [])
        if self._stack_limit and len(stack) > self._stack_limit:
            stack = stack[-self._stack_limit:]
        stack_text = "".join(stack)
        # Last 6 lines, each capped at 300 chars.
        tail_lines = [line.rstrip("\n") for line in "".join(stack).splitlines()]
        tail_lines = tail_lines[-6:]
        tail_lines = [line[:300] for line in tail_lines]
        return (stack_text, tail_lines)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "started_at": self._started_at,
                "updated_at": self._updated_at,
                "threshold_sec": self._threshold_sec,
                "blocks_total": self._blocks_total,
                "max_block_sec": self._max_block_sec,
                "last_block_sec": self._last_block_sec,
                "last_block_at": self._last_block_at,
                "last_block_stack_top": (
                    list(self._last_block_stack_top)
                    if self._last_block_stack_top is not None
                    else None
                ),
                "recent_blocks": [dict(item) for item in self._recent_blocks],
            }

    def _write_stats(self):
        self._last_stats_write = self._clock()
        try:
            data = self.snapshot()
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            path = self._stats_path
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
        except Exception as exc:
            try:
                self._logger.debug("LoopWatchdog: stats write failed: %s", exc)
            except Exception:  # pragma: no cover
                pass

    # ------------------------------------------------------------------- stop
    def stop(self):
        try:
            self._stop_event.set()
        except Exception:
            pass
        task = self._beat_task
        if task is not None:
            try:
                task.cancel()
            except Exception as exc:  # pragma: no cover
                self._logger.debug("LoopWatchdog: cancel failed: %s", exc)
        thread = self._thread
        if thread is not None:
            try:
                thread.join(timeout=2.0)
            except Exception as exc:  # pragma: no cover
                self._logger.debug("LoopWatchdog: join failed: %s", exc)
        # Final flush.
        try:
            self._write_stats()
        except Exception:  # pragma: no cover
            pass
