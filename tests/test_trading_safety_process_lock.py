import pytest

from trading_safety.process_lock import ProcessLock, ProcessLockError


def test_second_process_lock_is_rejected(tmp_path):
    first = ProcessLock(tmp_path / "bot.lock").acquire()
    try:
        with pytest.raises(ProcessLockError):
            ProcessLock(tmp_path / "bot.lock").acquire()
    finally:
        first.release()
