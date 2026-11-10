"""``limitpilot migrate`` — move from claude-account-pilot (the author's earlier tool).

Order matters, because a refresh token is single-use:

1. stop the old daemon first (stop marker under its lock, then launchd / Task Scheduler),
2. ask a running old dashboard to exit (its own nonce),
3. copy the state file (settings kept as they behaved before),
4. copy every slot credential and read each copy back; delete the old items only after
   every copy matched (an old copy left behind would turn into a dead token as soon as
   limitpilot rotates it),
5. point a gateway ``apiKeyHelper`` that still calls the old script at limitpilot,
6. remove leftovers of old isolated logins,
7. leave a ``MIGRATED`` marker in the old folder (the folder itself is kept).

``--dry-run`` prints the plan and changes nothing. If copying fails, the new copies are
removed again and the old items stay untouched.
"""

import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from limitpilot import __version__
from limitpilot.errors import KeychainError, LimitPilotError
from limitpilot import gateway
from limitpilot import i18n
from limitpilot import legacy
from limitpilot import locks
from limitpilot import paths
from limitpilot.store import fake
from limitpilot.store import filestore
from limitpilot.store import keychain

EVENTS = []   # 단계 기록(테스트·연습 모드 출력용), 실행마다 비운다


def _event(kind: str, detail: str = "") -> None:
    EVENTS.append((kind, detail))


def _say(msg: str) -> None:
    print(msg, flush=True)


def _legacy_state() -> dict:
    path = os.path.join(legacy.state_dir(), "state.json")
    try:
        data = filestore.read_json(path)
    except (OSError, ValueError) as e:
        raise LimitPilotError(i18n.t("mig_state_unreadable", path=path)) from e
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        raise LimitPilotError(i18n.t("mig_state_format"))
    return data


def _slots(st: dict):
    return sorted(int(k) for k, v in (st.get("slots") or {}).items() if v)


@contextmanager
def _legacy_lock(timeout: float = 30.0):
    """Hold the old tool's state lock (same flock file it uses)."""
    path = os.path.join(legacy.state_dir(), ".lock")
    with locks._file_lock(path, timeout, i18n.t("mig_old_running")):
        yield


# ---------------------------------------------------------------- 1·2단계: 이전 도구 멈추기

def stop_legacy_daemon() -> None:
    marker = os.path.join(legacy.state_dir(), ".daemon-stopped")
    with _legacy_lock():
        # 이전 데몬 틱은 이 표식을 보면 아무것도 하지 않고 끝난다
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        _event("legacy-stop-marker", marker)
    if paths.IS_WIN:
        subprocess.run(["schtasks", "/End", "/TN", legacy.WIN_TASK_NAME], capture_output=True)
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", legacy.WIN_TASK_NAME],
                       capture_output=True)
        _event("legacy-scheduler-removed", legacy.WIN_TASK_NAME)
    else:
        target = f"gui/{os.getuid()}/{legacy.launchd_label()}"
        subprocess.run(["launchctl", "bootout", target], capture_output=True)
        _event("legacy-bootout", target)
        for plist in legacy.launchd_plists():
            try:
                os.remove(plist)
                _event("legacy-plist-removed", plist)
            except FileNotFoundError:
                pass
    try:
        os.remove(legacy.daemon_script_path())
        _event("legacy-daemon-copy-removed", legacy.daemon_script_path())
    except FileNotFoundError:
        pass


def stop_legacy_dashboard() -> None:
    """Best effort: ask an old dashboard to exit with the nonce from its runtime record."""
    import urllib.request

    try:
        entries = list(os.scandir(legacy.state_dir()))
    except OSError:
        return
    for entry in entries:
        if not (entry.name.startswith("dashboard-runtime-") and entry.name.endswith(".json")):
            continue
        try:
            rec = filestore.read_json(entry.path)
            port, nonce = int(rec["port"]), str(rec["nonce"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/shutdown", data=json.dumps({"nonce": nonce}).encode(),
            headers={"Content-Type": "application/json", legacy.DASHBOARD_HEADER: "1"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=3):
                pass
            _event("legacy-dashboard-stopped", str(port))
        except Exception:  # noqa: BLE001 — 이미 꺼져 있으면 그만이다
            pass


# ---------------------------------------------------------------- 3·4단계: 복사

def _new_state(old: dict, daemon_was_installed: bool) -> dict:
    st = json.loads(json.dumps(old))   # 깊은 복사
    st["schemaVersion"] = 1
    st["lastNotify"] = {}
    for slot in (st.get("slots") or {}).values():
        if isinstance(slot, dict):
            slot.pop("fetchBackoff", None)
    settings = dict(st.get("settings") or {})
    # 이전 도구의 기본값은 유지 호출·리셋 전 소진이 켜져 있었다 — 그 사용자에게는
    # 전과 같은 동작을 유지한다(새 사용자 기본값은 꺼짐)
    settings.setdefault("keepalive", True)
    settings.setdefault("finalBurn", True)
    settings.setdefault("autoSwitch", bool(daemon_was_installed))
    st["settings"] = settings
    st["migratedFrom"] = {"tool": legacy.TOOL_NAME, "version": old.get("appVersion"),
                          "at": datetime.now(timezone.utc).isoformat(), "by": __version__}
    return st


def _copy_credentials(slots):
    """Copy each slot and verify by reading back. Returns the list of written slots."""
    written = []
    try:
        for n in slots:
            if paths.IS_WIN and not fake.active():
                src = os.path.join(legacy.state_dir(), "slots", f"slot-{n}.json")
                blob = filestore._file_read(src)
                if blob is None:
                    continue
                dst = filestore._slot_file(n)
                filestore._file_write(dst, blob)
                written.append(n)
                if filestore._file_read(dst) != blob:
                    raise KeychainError(i18n.t("mig_copy_verify", n=n))
            else:
                blob = keychain.kc_read(legacy.KEYCHAIN_SERVICE, paths.backup_account(n))
                if blob is None:
                    continue   # 게이트웨이 등 백업이 없는 슬롯
                keychain.kc_write(paths.BACKUP_SERVICE, paths.backup_account(n), blob)
                written.append(n)
                if keychain.kc_read(paths.BACKUP_SERVICE, paths.backup_account(n)) != blob:
                    raise KeychainError(i18n.t("mig_copy_readback", n=n))
            _event("copied", str(n))
    except Exception:
        _rollback(written)
        raise
    return written


def _rollback(written) -> None:
    """Remove the new copies so a retry starts clean; the old items were never touched."""
    for n in written:
        try:
            if paths.IS_WIN and not fake.active():
                os.remove(filestore._slot_file(n))
            else:
                keychain.kc_delete(paths.BACKUP_SERVICE, paths.backup_account(n))
            _event("rolled-back", str(n))
        except (OSError, KeychainError):
            pass


def _delete_legacy_credentials(slots) -> None:
    for n in slots:
        if paths.IS_WIN and not fake.active():
            try:
                os.remove(os.path.join(legacy.state_dir(), "slots", f"slot-{n}.json"))
            except FileNotFoundError:
                pass
        else:
            keychain.kc_delete(legacy.KEYCHAIN_SERVICE, paths.backup_account(n))
        _event("legacy-deleted", str(n))


# ---------------------------------------------------------------- 5·6단계

def fix_api_key_helper() -> bool:
    settings = gateway._read_claude_settings()
    helper = settings.get("apiKeyHelper")
    if not isinstance(helper, str):
        return False
    m = re.search(re.escape(legacy.STATE_DIRNAME) + r"[/\\]daemon[/\\]"
                  + re.escape(legacy.DAEMON_SCRIPT) + r"\"?'?\s+gateway-key\s+(\d+)", helper)
    if not m:
        return False
    settings["apiKeyHelper"] = gateway._gateway_helper_command(int(m.group(1)))
    gateway._write_claude_settings(settings)
    _event("api-key-helper", settings["apiKeyHelper"])
    return True


def remove_isolated_leftovers() -> int:
    from limitpilot.login import session

    removed = 0
    try:
        entries = list(os.scandir(legacy.state_dir()))
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith("oauth-login-") or not entry.is_dir(follow_symlinks=False):
            continue
        if not paths.IS_WIN:
            service = session.isolated_keychain_service(entry.path)
            if re.fullmatch(r"Claude Code-credentials-[0-9a-f]{8}", service):
                try:
                    keychain.kc_delete(service, paths.KC_USER)
                except KeychainError:
                    pass
        shutil.rmtree(entry.path, ignore_errors=True)
        removed += 1
        _event("isolated-removed", entry.name)
    return removed


# ---------------------------------------------------------------- 실행

def plan_lines(old: dict) -> list:
    slots = _slots(old)
    if paths.IS_WIN:
        scheduler = i18n.t("mig_plan_scheduler_win", name=legacy.WIN_TASK_NAME)
    else:
        # 화면·스크린샷에 로그인 사용자 이름이 찍히지 않게 자리표시자로
        scheduler = "launchctl bootout " + legacy.launchd_label().replace(getpass.getuser(), "<user>")
    return [
        i18n.t("mig_plan_1", marker=os.path.join(legacy.state_dir(), ".daemon-stopped"),
               scheduler=scheduler),
        i18n.t("mig_plan_2"),
        i18n.t("mig_plan_3", path=paths.state_file()),
        i18n.t("mig_plan_4", count=len(slots), slots=slots),
        i18n.t("mig_plan_5"),
        i18n.t("mig_plan_6"),
        i18n.t("mig_plan_7", path=os.path.join(legacy.state_dir(), legacy.MIGRATED_MARKER)),
    ]


def run(dry_run: bool = False, purge_legacy: bool = False) -> int:
    EVENTS.clear()
    if purge_legacy:
        return _purge_legacy(dry_run)
    if not legacy.has_data():
        _say("nothing to migrate: no claude-account-pilot data found")
        return 0
    if legacy.already_migrated():
        _say("already migrated — use `limitpilot migrate --purge-legacy` to delete the old folder")
        return 0
    old = _legacy_state()
    has_new_state = os.path.exists(paths.state_file())
    if dry_run:
        _say(i18n.t("mig_plan_header"))
        for line in plan_lines(old):
            _say("  " + line)
        if has_new_state:
            _say("  " + i18n.t("mig_plan_note", path=paths.state_file()))
        return 0
    if has_new_state:
        raise LimitPilotError(i18n.t("mig_state_exists", path=paths.state_file()))
    daemon_was_installed = any(os.path.exists(p) for p in legacy.launchd_plists()) or \
        os.path.exists(legacy.daemon_script_path())
    stop_legacy_daemon()
    stop_legacy_dashboard()
    with locks.state_lock():
        slots = _slots(old)
        written = _copy_credentials(slots)
        new_state = _new_state(old, daemon_was_installed)
        try:
            os.makedirs(paths.STATE_DIR, mode=0o700, exist_ok=True)
            filestore.atomic_write_json(paths.state_file(), new_state)
            _event("state-written", paths.state_file())
        except OSError:
            _rollback(written)
            raise
        # 모든 복사본을 확인한 뒤에만 이전 항목을 지운다
        _delete_legacy_credentials(written)
    fix_api_key_helper()
    remove_isolated_leftovers()
    with open(os.path.join(legacy.state_dir(), legacy.MIGRATED_MARKER), "w", encoding="utf-8") as f:
        f.write(f"migrated to limitpilot {__version__} at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    _event("marker", legacy.MIGRATED_MARKER)
    _say(f"migrated {len(written)} account(s) from claude-account-pilot. "
         "The old folder is kept (delete it later with `limitpilot migrate --purge-legacy`).")
    _say("The old menu bar app is not removed automatically — quit it and delete it yourself.")
    if daemon_was_installed:
        _say("Watching was on before: run `limitpilot install-daemon` to turn it on again.")
    return 0


def _purge_legacy(dry_run: bool) -> int:
    folder = legacy.state_dir()
    if not legacy.already_migrated():
        raise LimitPilotError(i18n.t("mig_purge_needs_migrate"))
    if os.path.islink(folder) or os.path.basename(folder) != legacy.STATE_DIRNAME:
        raise LimitPilotError(i18n.t("mig_purge_bad_path"))
    if dry_run:
        _say(f"would delete {folder}")
        return 0
    shutil.rmtree(folder)
    _say(f"deleted {folder}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run(dry_run="--dry-run" in sys.argv))
