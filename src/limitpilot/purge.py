"""`limitpilot purge`: remove the daemon, backup credentials, state and logs — never the
account Claude Code is signed in with (its credential, `.claude.json` and settings stay).
"""

import os
import re
import shutil
import subprocess
import sys
import time

from limitpilot.constants import (KC_NOT_FOUND_RC, KC_TIMEOUT_S, PURGE_KEYCHAIN_ITEM_LIMIT)
from limitpilot.errors import (LimitPilotError, KeychainError)
from limitpilot.daemon import deploy
from limitpilot.dashboard import runtime
from limitpilot import gateway
from limitpilot import i18n
from limitpilot import locks
from limitpilot.login import session
from limitpilot import paths
from limitpilot import state
from limitpilot.store import keychain


def _purge_isolated_keychain_items() -> int:
    """상태 디렉터리 안에 남은 임시 OAuth 프로필의 exact service만 삭제."""
    if paths.IS_WIN:
        return 0
    try:
        entries = list(os.scandir(paths.STATE_DIR))
    except FileNotFoundError:
        return 0
    except OSError as e:
        raise LimitPilotError(i18n.t("purge_tmp_profile")) from e
    deleted = 0
    for entry in entries:
        if (not entry.name.startswith("oauth-login-")
                or not entry.is_dir(follow_symlinks=False)):
            continue
        service = session.isolated_keychain_service(entry.path)
        if not re.fullmatch(r"Claude Code-credentials-[0-9a-f]{8}", service):
            raise LimitPilotError(i18n.t("purge_tmp_service"))
        if keychain.kc_delete(service, paths.KC_USER):
            deleted += 1
    return deleted


def _keychain_service_exists(service: str) -> bool:
    last_error = None
    for attempt in range(3):
        try:
            result = subprocess.run(
                [paths.SECURITY, "find-generic-password", "-s", service],
                capture_output=True, text=True, timeout=KC_TIMEOUT_S,
            )
            if result.returncode == 0:
                return True
            if result.returncode == KC_NOT_FOUND_RC:
                return False
            last_error = f"rc={result.returncode}"
        except subprocess.TimeoutExpired:
            last_error = i18n.t("timeout_s", s=KC_TIMEOUT_S)
        except OSError as e:
            last_error = type(e).__name__
        if attempt < 2:
            time.sleep(0.2)
    raise KeychainError(i18n.t("purge_sec_verify", err=last_error))


def _purge_backup_keychain_items() -> int:
    """state가 손상됐어도 exact backup service의 모든 항목을 삭제한다."""
    if paths.IS_WIN:
        return 0  # Windows 슬롯은 STATE_DIR 아래의 파일이다.
    deleted = 0
    for _ in range(PURGE_KEYCHAIN_ITEM_LIMIT):
        if not keychain.kc_delete(paths.BACKUP_SERVICE):
            break
        deleted += 1
    else:
        raise KeychainError(i18n.t("purge_too_many"))
    if _keychain_service_exists(paths.BACKUP_SERVICE):
        raise KeychainError(i18n.t("purge_kc_left"))
    return deleted


def _validate_state_root_for_purge() -> str:
    """Only ``<home>/.limitpilot`` or an explicit ``LIMITPILOT_HOME`` may be deleted."""
    actual = os.path.abspath(paths.STATE_DIR)
    allowed = {os.path.abspath(os.path.join(paths.HOME, ".limitpilot"))}
    override = os.environ.get("LIMITPILOT_HOME")
    if override:
        allowed.add(os.path.abspath(override))
    dangerous = {os.path.abspath(paths.HOME), os.path.abspath(os.path.expanduser("~")),
                 os.path.abspath(os.sep)}
    if actual not in allowed or actual in dangerous:
        raise LimitPilotError(i18n.t("purge_bad_root"))
    if os.path.islink(actual):
        raise LimitPilotError(i18n.t("purge_symlink"))
    return actual


def _remove_state_tree(state_root: str) -> None:
    try:
        shutil.rmtree(state_root)
    except FileNotFoundError:
        return
    except OSError as e:
        try:
            remaining = sorted(entry.name for entry in os.scandir(state_root))[:5]
        except OSError:
            remaining = []
        detail = i18n.t("purge_remaining", names=", ".join(remaining)) if remaining else ""
        raise LimitPilotError(i18n.t("purge_rm_failed") + detail) from e
    if os.path.lexists(state_root):
        raise LimitPilotError(i18n.t("purge_dir_left"))


def _confirm_purge() -> bool:
    print(i18n.t("purge_warning"), file=sys.stderr)
    try:
        return input(i18n.t("purge_prompt")).strip() == "PURGE"
    except (EOFError, KeyboardInterrupt):
        return False


def cmd_purge(args) -> int:
    """도구가 소유한 데몬·백업·상태·로그만 완전 삭제한다."""
    state_root = _validate_state_root_for_purge()
    if not args.yes and not _confirm_purge():
        print(i18n.t("purge_cancelled"), file=sys.stderr)
        return 0

    # 스캔보다 표식을 먼저 세워 스캔 직후 새로 시작하는 dashboard/tick도
    # 자체적으로 종료하게 한다. 실패 시에는 안전을 위해 표식을 남긴다.
    os.makedirs(paths.STATE_DIR, mode=0o700, exist_ok=True)
    marker = os.path.join(paths.STATE_DIR, ".purging")
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
    except OSError as e:
        raise LimitPilotError(i18n.t("purge_marker")) from e

    # 대시보드 OAuth worker가 purge 직후 state/Keychain을 다시 만드는 경합 방지.
    runtime._stop_running_dashboards()
    with locks.state_lock():
        # 진행 중 틱이 락을 놓은 후 스케줄러를 해제하면 새 틱이 들어오지 못한다.
        deploy._stop_daemon(strict=True)
        st = state.load_state()
        if st.get("gatewayRestore") is not None:
            gateway.deactivate_gateway_settings(st)
        _purge_isolated_keychain_items()
        _purge_backup_keychain_items()

    # /End 직전에 이미 state lock을 기다리던 틱에 한번 양보하고,
    # marker를 본 틱이 종료한 뒤 최종 삭제한다.
    time.sleep(0.25)
    with locks.state_lock(timeout=5.0):
        pass
    runtime._wait_for_dashboard_processes_to_exit(timeout=0.0)

    # Windows에서는 락 fd가 열려 있을 때 트리를 지울 수 없으므로 해제 후 삭제.
    _remove_state_tree(state_root)
    print(i18n.t("purge_done"))
    return 0
