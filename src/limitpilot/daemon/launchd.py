"""macOS launchd agent: register / remove the 60-second daemon."""

import os
import plistlib
import subprocess

from limitpilot.constants import EXIT_ERROR, LAUNCHCTL_NOT_FOUND_RC
from limitpilot.errors import LimitPilotError
from limitpilot import i18n
from limitpilot import logutil
from limitpilot import paths as paths_mod


def build_plist(python: str, pyz: str) -> bytes:
    """launchd plist for ``python limitpilot.pyz auto --once`` every 60 seconds.

    Built with plistlib, so paths containing ``&`` or ``<`` are escaped correctly.
    stdout goes nowhere; stderr (errors only in daemon mode) goes to a capped log.
    """
    state = paths_mod.STATE_DIR
    return plistlib.dumps({
        "Label": paths_mod.LAUNCHD_LABEL,
        "ProgramArguments": [python, pyz, "auto", "--once"],
        "StartInterval": 60,
        "RunAtLoad": True,
        # 데몬 모드 표식 — log() 가 stderr 에 오류만 쓰게 한다(daemon.err.log 폭증 방지)
        "EnvironmentVariables": {"LIMITPILOT_DAEMON": "1"},
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": os.path.join(state, "daemon.err.log"),
        "ProcessType": "Background",
    })


def install(python: str, pyz: str) -> int:
    plist = build_plist(python, pyz)
    # 정위치는 ~/Library/LaunchAgents(로그인 시 자동 로드). 이 폴더가 root 소유인 PC 가
    # 있어서, 쓰기가 안 되면 상태 폴더에 두고 현재 세션에만 등록한다(재부팅 후 재등록 필요).
    plist_path = paths_mod.LAUNCHD_PLIST
    persistent = True
    try:
        os.makedirs(os.path.dirname(paths_mod.LAUNCHD_PLIST), exist_ok=True)
        with open(paths_mod.LAUNCHD_PLIST, "wb") as f:
            f.write(plist)
    except PermissionError:
        persistent = False
        plist_path = os.path.join(paths_mod.STATE_DIR, paths_mod.LAUNCHD_LABEL + ".plist")
        with open(plist_path, "wb") as f:
            f.write(plist)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{paths_mod.LAUNCHD_LABEL}"],
                   capture_output=True)  # 기존 등록 제거(멱등)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", plist_path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        r = subprocess.run(["launchctl", "load", "-w", plist_path],
                           capture_output=True, text=True)
        if r.returncode != 0:
            logutil.error(i18n.t("ld_register_failed", err=r.stderr.strip()))
            return EXIT_ERROR
    logutil.log(i18n.t("ld_registered", label=paths_mod.LAUNCHD_LABEL))
    if not persistent:
        logutil.log(i18n.t("ld_not_persistent") + f"\n  sudo chown {paths_mod.KC_USER} ~/Library/LaunchAgents")
    return 0


def _mac_uninstall_daemon(strict: bool = False) -> None:
    uid = os.getuid()
    domain_target = f"gui/{uid}/{paths_mod.LAUNCHD_LABEL}"
    try:
        before = subprocess.run(
            ["launchctl", "print", domain_target], capture_output=True,
        ) if strict else None
        if strict and before.returncode not in (0, LAUNCHCTL_NOT_FOUND_RC):
            raise LimitPilotError(i18n.t("ld_status_unknown"))
        subprocess.run(["launchctl", "bootout", domain_target], capture_output=True)
    except OSError as e:
        raise LimitPilotError(i18n.t("ld_remove_failed")) from e
    paths = (paths_mod.LAUNCHD_PLIST,
             os.path.join(paths_mod.STATE_DIR, paths_mod.LAUNCHD_LABEL + ".plist"))
    for path in paths:
        try:
            subprocess.run(["launchctl", "unload", path], capture_output=True)
        except OSError as e:
            if strict:
                raise LimitPilotError(i18n.t("ld_unload_failed")) from e
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            if strict:
                raise LimitPilotError(i18n.t("ld_file_delete_failed", path=path)) from e
    if strict:
        try:
            query = subprocess.run(
                ["launchctl", "print", domain_target], capture_output=True,
            )
        except OSError as e:
            raise LimitPilotError(i18n.t("ld_verify_failed")) from e
        if query.returncode != LAUNCHCTL_NOT_FOUND_RC:
            raise LimitPilotError(i18n.t("ld_not_removed"))
        remaining = [path for path in paths if os.path.lexists(path)]
        if remaining:
            raise LimitPilotError(i18n.t("ld_file_left", path=remaining[0]))
