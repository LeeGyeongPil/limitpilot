"""Records of running dashboards (``dashboard-runtime-<pid>.json``) and how to stop them.

A dashboard is stopped only through its own ``/api/shutdown`` with the nonce from its
runtime record, after its ``/api/health`` reports the same instance id.
"""

import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from limitpilot.errors import (LimitPilotError)
from limitpilot import i18n
from limitpilot import paths
from limitpilot.store import filestore


def _dashboard_health(port: int):
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=1,
        ) as response:
            data = json.loads(response.read(4096).decode("utf-8"))
        if data.get("app") == paths.DASHBOARD_APP_ID and data.get("ok") is True:
            return data
    except Exception:
        pass
    return None


def _dashboard_runtime_records():
    """도구가 직접 시작한 대시보드 인스턴스 기록만 반환한다."""
    try:
        entries = list(os.scandir(paths.STATE_DIR))
    except FileNotFoundError:
        return []
    except OSError as e:
        raise LimitPilotError(i18n.t("dr_state")) from e
    records = []
    for entry in entries:
        if (not entry.name.startswith(paths.DASHBOARD_RUNTIME_PREFIX)
                or not entry.name.endswith(".json")
                or not entry.is_file(follow_symlinks=False)):
            continue
        try:
            if entry.stat(follow_symlinks=False).st_size > 4096:
                continue
            record = filestore.read_json(entry.path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict) or record.get("app") != paths.DASHBOARD_APP_ID:
            continue
        pid = record.get("pid")
        port, nonce = record.get("port"), record.get("nonce")
        instance = record.get("instance")
        if (isinstance(port, int) and 1 <= port <= 65535
                and isinstance(pid, int) and pid > 0
                and isinstance(nonce, str) and len(nonce) >= 24
                and isinstance(instance, str) and len(instance) >= 16):
            records.append((entry.path, pid, port, nonce, instance))
    return records


def _process_exists(pid: int) -> bool:
    if paths.IS_WIN:
        # Windows의 os.kill(pid, 0)은 POSIX 존재 확인이 아니라 TerminateProcess로
        # 연결될 수 있으므로 절대 쓰지 않는다.
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True,
            )
        except OSError as e:
            raise LimitPilotError(i18n.t("dr_proc_check_failed")) from e
        if result.returncode != 0:
            raise LimitPilotError(i18n.t("dr_proc_state"))
        try:
            return any(
                len(row) > 1 and row[1].strip() == str(pid)
                for row in csv.reader((result.stdout or "").splitlines())
            )
        except csv.Error as e:
            raise LimitPilotError(i18n.t("dr_proc_parse")) from e
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as e:
        raise LimitPilotError(i18n.t("dr_proc_state")) from e


# `python -m limitpilot dashboard`, `limitpilot dashboard`, `python limitpilot.pyz dashboard`
# (전역 옵션 --demo/--lang 이 사이에 올 수 있다). 이 패턴은 경고에만 쓴다 — 종료 대상은
# 런타임 기록의 pid·instance 가 확인된 프로세스뿐이다.
_DASHBOARD_PROCESS_RE = re.compile(
    r"(?:-m\s+limitpilot|limitpilot(?:\.pyz)?[\"']?)(?:\s+--?\S+)*\s+dashboard(?:\s|$)",
    re.IGNORECASE,
)


def _dashboard_process_pids():
    """Best-effort scan for dashboards started before they wrote a runtime record."""
    if paths.IS_WIN:
        script = (
            "$ErrorActionPreference='Stop'; "
            "Get-CimInstance Win32_Process | "
            r"Where-Object { $_.Name -match '^(py|pythonw?|python[0-9.]*|limitpilot)\.exe$' -and "
            "$_.CommandLine -like '*limitpilot*dashboard*' } | "
            "ForEach-Object { $_.ProcessId }"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise LimitPilotError(i18n.t("dr_list_failed")) from e
        if result.returncode != 0:
            raise LimitPilotError(i18n.t("dr_list"))
        return sorted({
            int(line.strip()) for line in (result.stdout or "").splitlines()
            if line.strip().isdigit() and int(line.strip()) != os.getpid()
        })

    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,comm=,command="],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise LimitPilotError(i18n.t("dr_list_failed")) from e
    if result.returncode != 0:
        raise LimitPilotError(i18n.t("dr_list"))
    pids = set()
    for line in (result.stdout or "").splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3 or not fields[0].isdigit():
            continue
        pid, executable, command = int(fields[0]), os.path.basename(fields[1]), fields[2]
        exe = executable.casefold()
        if (pid != os.getpid() and (exe.startswith("python") or exe.startswith("limitpilot"))
                and _DASHBOARD_PROCESS_RE.search(command)):
            pids.add(pid)
    return sorted(pids)


def _wait_for_dashboard_processes_to_exit(timeout: float = 3.0):
    """Wait a little for unrecorded dashboards to exit; return the pids still running.

    This never kills anything: a name match is not proof that a process is ours.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            left = _dashboard_process_pids()
        except LimitPilotError:
            return []  # 목록을 못 얻으면 경고도 생략(기록된 인스턴스는 이미 처리됨)
        if not left or time.monotonic() >= deadline:
            return left
        time.sleep(0.1)


def _stop_running_dashboards() -> None:
    """자격증명을 다시 저장할 수 있는 대시보드 워커를 purge 전에 종료한다."""
    stopped_ports = set()
    for _path, pid, port, nonce, instance in _dashboard_runtime_records():
        if port in stopped_ports:
            continue
        health = _dashboard_health(port)
        if not health and _process_exists(pid):
            # runtime 기록은 serve_forever 직전에 쓴다. 정상 시작 중인
            # 짧은 구간을 stale로 오판하지 않고 health를 기다린다.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not health:
                time.sleep(0.1)
                health = _dashboard_health(port)
            if not health:
                raise LimitPilotError(i18n.t("dr_unknown_status"))
        if not health:
            continue  # 종료된 PID의 stale 기록
        if health.get("instance") != instance:
            continue  # 포트가 새 인스턴스/다른 프로세스에 재사용됨
        body = json.dumps({"nonce": nonce}).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/shutdown",
            data=body,
            headers={"Content-Type": "application/json", "X-LimitPilot": "1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                if response.status != 200:
                    raise LimitPilotError(i18n.t("dr_stop_bad_status"))
        except LimitPilotError:
            raise
        except Exception as e:
            raise LimitPilotError(i18n.t("dr_stop_failed")) from e
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _dashboard_health(port):
            time.sleep(0.1)
        if _dashboard_health(port):
            raise LimitPilotError(i18n.t("dr_stop_timeout"))
        stopped_ports.add(port)

    # 1.1.1 이전 대시보드는 runtime 기록이 없다. 기본 포트에
    # 예전 서버가 남아 있으면 상태 재생성 경합을 피해 삭제를 거부한다.
    if (paths.DASHBOARD_DEFAULT_PORT not in stopped_ports
            and _dashboard_health(paths.DASHBOARD_DEFAULT_PORT)):
        raise LimitPilotError(i18n.t("dr_close_then_purge"))
    left = _wait_for_dashboard_processes_to_exit()
    if left:
        print(i18n.t("dr_orphans", pids=left), file=sys.stderr)
