"""Activity log with a hard size cap.

* ``limitpilot.log`` rotates at ``LOG_MAX_BYTES`` and keeps ``LOG_BACKUPS`` old files,
  so the total never exceeds ``LOG_MAX_BYTES * (LOG_BACKUPS + 1)``.
* In daemon mode (``LIMITPILOT_DAEMON=1``, set by the launchd plist / scheduled task)
  only errors go to stderr. launchd appends stderr to ``daemon.err.log``; logging every
  "no action" tick there used to grow that file by ~1,440 lines a day, forever.
* ``daemon.err.log`` itself is rotated with the same cap at the start of each tick.

Tokens are never logged.
"""

import os
import sys
from datetime import datetime

from limitpilot import paths

LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 3


def daemon_mode() -> bool:
    return os.environ.get("LIMITPILOT_DAEMON") == "1"


def rotate(path: str, max_bytes=None, backups=None) -> None:
    """``path`` → ``path.1`` → … → ``path.N`` (the oldest is dropped) once it is too big."""
    max_bytes = LOG_MAX_BYTES if max_bytes is None else max_bytes
    backups = LOG_BACKUPS if backups is None else backups
    try:
        if os.path.getsize(path) <= max_bytes:
            return
    except OSError:
        return
    for i in range(backups, 0, -1):
        src = path if i == 1 else f"{path}.{i - 1}"
        try:
            os.replace(src, f"{path}.{i}")
        except FileNotFoundError:
            pass


def _write(line: str) -> None:
    try:
        os.makedirs(paths.STATE_DIR, mode=0o700, exist_ok=True)
        target = paths.log_file()
        rotate(target)
        with open(target, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def log(msg: str) -> None:
    """Informational line: log file always, stderr only outside daemon mode."""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    if not daemon_mode() and os.environ.get("LIMITPILOT_QUIET") != "1":
        print(line, file=sys.stderr)
    _write(line)


def error(msg: str) -> None:
    """Error line: log file and stderr (also in daemon mode)."""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, file=sys.stderr)
    _write(line)


def rotate_daemon_err_log() -> None:
    rotate(os.path.join(paths.STATE_DIR, "daemon.err.log"))


def total_log_bytes() -> int:
    """Bytes used by limitpilot.log and its rotated copies (for tests and doctor)."""
    total = 0
    base = paths.log_file()
    for name in [base] + [f"{base}.{i}" for i in range(1, LOG_BACKUPS + 1)]:
        try:
            total += os.path.getsize(name)
        except OSError:
            pass
    return total
