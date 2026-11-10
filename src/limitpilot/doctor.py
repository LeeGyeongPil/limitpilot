"""``limitpilot doctor`` — read-only environment check.

Nothing here writes, switches or calls the network; it only looks at files, versions and
the dashboard port, and prints one line per check (``ok`` / ``warn`` / ``fail``).
"""

import json
import os
import socket
import sys

from limitpilot import __version__
from limitpilot.daemon import deploy
from limitpilot import legacy
from limitpilot import logutil
from limitpilot import paths
from limitpilot import store


def _line(level: str, what: str, detail: str = "") -> None:
    print(f"{level:<5} {what}" + (f" — {detail}" if detail else ""))


def run() -> int:
    failures = 0
    _line("ok", f"limitpilot {__version__}", f"Python {sys.version.split()[0]} on {sys.platform}")
    if sys.version_info < (3, 10):
        _line("fail", "Python 3.10 or newer is required")
        failures += 1
    if sys.platform not in ("darwin", "win32"):
        _line("warn", "switching works on macOS and Windows only")
    _line("ok", "credential store", store.backend())

    # Claude Code 파일 — 형식이 바뀌면 전환이 깨질 수 있다
    try:
        with open(paths.CLAUDE_JSON, encoding="utf-8") as handle:
            account = (json.load(handle) or {}).get("oauthAccount") or {}
        if account.get("accountUuid") and account.get("emailAddress"):
            _line("ok", "~/.claude.json", "oauthAccount found")
        else:
            _line("warn", "~/.claude.json", "no oauthAccount — sign in to Claude Code first")
    except FileNotFoundError:
        _line("warn", "~/.claude.json not found", "is Claude Code installed and signed in?")
    except (OSError, ValueError):
        _line("fail", "~/.claude.json is not readable JSON")
        failures += 1

    # 감시 데몬
    pyz = deploy.deployed_daemon_script_path()
    if os.path.exists(pyz):
        deployed = deploy.script_app_version(pyz)
        mine = deploy.version_tuple(__version__)
        if deployed and deployed != mine:
            _line("warn", "daemon copy version", f"{'.'.join(map(str, deployed))} ≠ {__version__}"
                  " — any limitpilot command refreshes it")
        else:
            _line("ok", "daemon copy", pyz)
    else:
        _line("ok", "daemon not installed", "`limitpilot install-daemon` turns on watching")
    for trace in legacy.running_legacy_daemons():
        _line("warn", "claude-account-pilot daemon still installed", f"{trace} — run `limitpilot migrate`")
    if legacy.migration_pending():
        _line("warn", "claude-account-pilot data found", "run `limitpilot migrate --dry-run`")

    # 로그 용량
    _line("ok", "activity log", f"{logutil.total_log_bytes()} bytes "
          f"(cap {logutil.LOG_MAX_BYTES * (logutil.LOG_BACKUPS + 1)})")

    # 대시보드 포트
    port = paths.DASHBOARD_DEFAULT_PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        busy = s.connect_ex(("127.0.0.1", port)) == 0
    _line("ok", f"dashboard port {port}", "in use (a dashboard may be running)" if busy else "free")
    return 1 if failures else 0
