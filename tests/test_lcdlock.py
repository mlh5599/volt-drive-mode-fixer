"""Advisory LCD hand-off lock."""

import os

import pytest

from voltdmf import lcdlock


@pytest.fixture
def lock_path(tmp_path):
    return tmp_path / "lcd.lock"


def test_holder_none_when_absent(lock_path):
    assert lcdlock.holder(lock_path) is None
    assert lcdlock.is_held_by_other(lock_path) is False


def test_claim_and_release_roundtrip(lock_path):
    assert lcdlock.claim("drive_log.py", lock_path) is True
    line = lcdlock.holder(lock_path)
    assert line == f"{os.getpid()}: drive_log.py"
    # our own claim is not "someone else"
    assert lcdlock.is_held_by_other(lock_path) is False
    lcdlock.release(lock_path)
    assert lcdlock.holder(lock_path) is None


def test_hold_context_manager_clears_on_exit(lock_path):
    with lcdlock.hold("lcd.py --watch", lock_path):
        assert lcdlock.holder(lock_path).endswith("lcd.py --watch")
    assert lcdlock.holder(lock_path) is None


def test_live_other_pid_is_held_by_other(lock_path):
    lock_path.write_text("1: someone-else")  # pid 1 is always alive
    assert lcdlock.is_held_by_other(lock_path) is True
    assert lcdlock.holder(lock_path) == "1: someone-else"


def test_stale_pid_is_ignored_and_file_removed(lock_path):
    dead = _a_dead_pid()
    lock_path.write_text(f"{dead}: gone")
    assert lcdlock.holder(lock_path) is None
    assert not lock_path.exists()  # stale file cleared


def test_release_leaves_another_owners_file_alone(lock_path):
    lock_path.write_text("1: someone-else")
    lcdlock.release(lock_path)
    assert lock_path.exists()


def _a_dead_pid() -> int:
    """A PID that is (almost certainly) not running."""
    for pid in range(999_999, 900_000, -1):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            continue
    raise AssertionError("could not find a free PID")
