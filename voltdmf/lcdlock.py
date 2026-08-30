"""Advisory single-writer lock for the LCD serial line.

Only one process can sensibly drive the SparkFun display on ``/dev/serial0``
at a time. The daemon paints an idle *watch* screen whenever nothing else
wants the panel; a purpose-driven tool (``tools/lcd.py``, ``drive_log.py
--lcd``, ``soc_log.py --lcd``) that needs the screen calls :func:`hold`,
and the daemon's dashboard notices, closes its port, and idles until the
lock is released.

The lock is just a small file at :data:`LOCK_PATH` holding
``"<pid>: <what>"``. It is advisory -- nothing enforces it but the daemon
checking :func:`holder` each repaint. A lock left behind by a dead process
(pid no longer alive) is treated as absent.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

def _default_lock_path() -> Path:
    """Where the hand-off file lives when ``VOLTDMF_LCD_LOCK`` is unset.

    On the Pi the daemon runs as ``voltdmf`` under ``ProtectHome=`` /
    ``ProtectSystem=strict`` while the by-hand tools run as the login user
    out of ``~/vdmf`` -- they share no writable ``$HOME``. ``/run/lock`` (FHS,
    tmpfs, ``1777``) is the one spot both can always reach, so prefer it and
    fall back to a ``$HOME`` dotfile only where it is missing (dev boxes,
    non-Linux). Stale files there are owned by whoever last held the lock;
    the sticky bit can stop the daemon unlinking one it did not create, but
    :func:`holder` treats a dead-pid file as absent regardless.
    """
    override = os.environ.get("VOLTDMF_LCD_LOCK")
    if override:
        return Path(override)
    run_lock = Path("/run/lock")
    if run_lock.is_dir():
        return run_lock / "voltdmf-lcd.lock"
    return Path.home() / ".voltdmf-lcd.lock"


#: Advisory hand-off file, ``"<pid>: <what>"``. Override with
#: ``VOLTDMF_LCD_LOCK`` (the deployed unit sets it; tests point it at a tmp
#: path). Reassigning this module attribute also works -- callers read it
#: lazily via ``path or LOCK_PATH``.
LOCK_PATH = _default_lock_path()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def holder(path: Path | None = None) -> str | None:
    """Return the ``"<pid>: <what>"`` line if the LCD is claimed by a live
    process, else ``None`` (also clearing a stale file)."""
    p = path or LOCK_PATH
    try:
        text = p.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    pid_str, _, _ = text.partition(":")
    try:
        pid = int(pid_str)
    except ValueError:
        return text or None
    if pid == os.getpid():
        return text
    if not _pid_alive(pid):
        with contextlib.suppress(OSError):
            p.unlink()
        return None
    return text


def is_held_by_other(path: Path | None = None) -> bool:
    """True if some *other* live process holds the LCD lock."""
    line = holder(path)
    if line is None:
        return False
    pid_str, _, _ = line.partition(":")
    return pid_str.strip() != str(os.getpid())


def claim(what: str, path: Path | None = None) -> bool:
    """Write this process's LCD claim. Best-effort: returns False (and the
    caller just proceeds) if the file cannot be written -- the daemon
    dashboard is the only reader and it will simply share the port."""
    p = path or LOCK_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{os.getpid()}: {what}", encoding="utf-8")
        return True
    except OSError:
        return False


def release(path: Path | None = None) -> None:
    """Drop this process's LCD claim (leaves another owner's file alone)."""
    p = path or LOCK_PATH
    line = holder(p)
    if line is not None and line.startswith(f"{os.getpid()}:"):
        with contextlib.suppress(OSError):
            p.unlink()


@contextlib.contextmanager
def hold(what: str, path: Path | None = None):
    """Claim the LCD for the duration of the ``with`` block."""
    wrote = claim(what, path)
    try:
        yield
    finally:
        if wrote:
            release(path)
