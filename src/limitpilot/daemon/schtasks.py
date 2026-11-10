"""Windows Task Scheduler: register / remove the 1-minute daemon task."""

import csv
import subprocess

from limitpilot.constants import EXIT_ERROR
from limitpilot.errors import LimitPilotError
from limitpilot.daemon import deploy
from limitpilot import i18n
from limitpilot import logutil
from limitpilot import paths


def _win_install_daemon(daemon_script: str) -> int:
    """Register a 1-minute ``auto --once`` task (the launchd StartInterval equivalent)."""
    # --daemon: stderr 에는 오류만 쓴다(launchd 쪽은 plist 환경변수로 같은 효과)
    tr = f'"{deploy.find_python()}" "{daemon_script}" --daemon auto --once'
    r = subprocess.run(
        ["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", "1",
         "/TN", paths.WIN_TASK_NAME, "/TR", tr],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        logutil.log(i18n.t("st_register_failed", err=(r.stderr or r.stdout).strip()))
        return EXIT_ERROR
    logutil.log(i18n.t("st_registered", name=paths.WIN_TASK_NAME))
    return 0


def _win_task_exists_strict() -> bool:
    """Task Scheduler 전체 조회가 성공했을 때만 exact task 존재를 판정한다.

    `/Query /TN`은 '없음'과 RPC/권한 장애가 모두 nonzero라 구분할 수 없다.
    반면 전체 CSV 조회가 실패하면 스케줄러 상태 검증 실패로 처리할 수 있다.
    """
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/FO", "CSV", "/NH"],
            capture_output=True, text=True,
        )
    except OSError as e:
        raise LimitPilotError(i18n.t("st_check_failed")) from e
    if result.returncode != 0:
        raise LimitPilotError(i18n.t("st_status_unknown"))
    wanted = paths.WIN_TASK_NAME.casefold()
    try:
        rows = csv.reader((result.stdout or "").splitlines())
        return any(
            row and row[0].strip().lstrip("\ufeff\\").casefold() == wanted
            for row in rows
        )
    except csv.Error as e:
        raise LimitPilotError(i18n.t("st_parse_failed")) from e


def _win_uninstall_daemon(strict: bool = False) -> None:
    if not strict:
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", paths.WIN_TASK_NAME],
                       capture_output=True)
        return
    if not _win_task_exists_strict():
        return
    # 대기/실행 중인 auto --once를 먼저 끝낸 뒤 정의를 지운다.
    # /End는 작업이 실행 중이 아니면 nonzero이므로 반환코드는 직접
    # 성공 판정에 쓰지 않고, 정의 삭제와 사후 전체 조회로 검증한다.
    try:
        subprocess.run(["schtasks", "/End", "/TN", paths.WIN_TASK_NAME],
                       capture_output=True)
        deleted = subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", paths.WIN_TASK_NAME],
            capture_output=True,
        )
    except OSError as e:
        raise LimitPilotError(i18n.t("st_remove_failed")) from e
    if deleted.returncode != 0:
        raise LimitPilotError(i18n.t("st_not_removed"))
    if _win_task_exists_strict():
        raise LimitPilotError(i18n.t("st_left"))
