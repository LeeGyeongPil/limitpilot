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
from limitpilot import i18n
from limitpilot import legacy
from limitpilot import logutil
from limitpilot import paths
from limitpilot import store


def _line(level: str, what: str, detail: str = "") -> None:
    print(f"{level:<5} {what}" + (f" — {detail}" if detail else ""))


def run() -> int:
    failures = 0
    _line("ok", i18n.t("doc_version", version=__version__),
          i18n.t("doc_version_detail", python=sys.version.split()[0], platform=sys.platform))
    if sys.version_info < (3, 10):
        _line("fail", i18n.t("doc_python_old"))
        failures += 1
    if sys.platform not in ("darwin", "win32"):
        _line("warn", i18n.t("doc_platform"))
    _line("ok", i18n.t("doc_store"), store.backend())

    # Claude Code 파일 — 형식이 바뀌면 전환이 깨질 수 있다
    try:
        with open(paths.CLAUDE_JSON, encoding="utf-8") as handle:
            account = (json.load(handle) or {}).get("oauthAccount") or {}
        if account.get("accountUuid") and account.get("emailAddress"):
            _line("ok", i18n.t("doc_cj_file"), i18n.t("doc_cj_ok"))
        else:
            _line("warn", i18n.t("doc_cj_file"), i18n.t("doc_cj_no_account"))
    except FileNotFoundError:
        _line("warn", i18n.t("doc_cj_missing"), i18n.t("doc_cj_missing_detail"))
    except (OSError, ValueError):
        _line("fail", i18n.t("doc_cj_unreadable"))
        failures += 1

    # 감시 데몬
    pyz = deploy.deployed_daemon_script_path()
    if os.path.exists(pyz):
        deployed = deploy.script_app_version(pyz)
        mine = deploy.version_tuple(__version__)
        if deployed and deployed != mine:
            _line("warn", i18n.t("doc_daemon_version"),
                  i18n.t("doc_daemon_version_detail", old=".".join(map(str, deployed)), new=__version__))
        else:
            _line("ok", i18n.t("doc_daemon_copy"), pyz)
    else:
        _line("ok", i18n.t("doc_daemon_none"), i18n.t("doc_daemon_none_detail"))
    for trace in legacy.running_legacy_daemons():
        _line("warn", i18n.t("doc_legacy_daemon"), i18n.t("doc_legacy_daemon_detail", trace=trace))
    if legacy.migration_pending():
        _line("warn", i18n.t("doc_legacy_data"), i18n.t("doc_legacy_data_detail"))

    # 로그 용량
    _line("ok", i18n.t("doc_log"), i18n.t(
        "doc_log_detail", size=logutil.total_log_bytes(),
        cap=logutil.LOG_MAX_BYTES * (logutil.LOG_BACKUPS + 1)))

    # 대시보드 포트
    port = paths.DASHBOARD_DEFAULT_PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        busy = s.connect_ex(("127.0.0.1", port)) == 0
    _line("ok", i18n.t("doc_port", port=port), i18n.t("doc_port_busy" if busy else "doc_port_free"))
    return 1 if failures else 0
