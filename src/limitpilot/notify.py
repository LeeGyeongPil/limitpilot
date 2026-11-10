"""OS notifications (macOS osascript, Windows toast) with an hourly de-duplication."""

import os
import subprocess
import time

from limitpilot.constants import NOTIFY_DEDUPE_S
from limitpilot import paths


# Windows 토스트 — 제목/본문은 환경변수로 전달해 스크립트 인젝션을 원천 차단.
# AUMID는 모든 Windows에 기본 등록된 PowerShell의 것을 차용한다 — 미등록 임의
# AUMID로는 토스트가 무음 실패하고, 자체 등록은 시작메뉴 바로가기 설치가 필요.
_WIN_TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $t.GetElementsByTagName('text')
$texts.Item(0).AppendChild($t.CreateTextNode($env:LP_TITLE)) | Out-Null
$texts.Item(1).AppendChild($t.CreateTextNode($env:LP_BODY)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($t)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier(
    '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
).Show($toast)
"""


SENT = []          # 샌드박스·연습 모드에서 보낸 알림 기록(최근 100개)


def notify(title: str, body: str) -> None:
    """OS notification. Practice mode prints it instead (and records it in ``SENT``).

    In the test sandbox a real osascript / PowerShell call is blocked by the sandbox
    hook, so tests patch this function or ``subprocess.run``.
    """
    if os.environ.get("LIMITPILOT_DEMO") == "1":
        SENT.append((title, body))
        del SENT[:-100]
        print(f"      [notification] {title} — {body}")
        return
    try:
        if paths.IS_WIN:
            env = dict(os.environ, LP_TITLE=title, LP_BODY=body)
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", _WIN_TOAST_PS],
                capture_output=True, timeout=15, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        # osascript 소스 문자열 내 이스케이프 — email 등 임의 텍스트 안전 처리
        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')

        script = f'display notification "{esc(body)}" with title "{esc(title)}"'
        subprocess.run([paths.OSASCRIPT, "-e", script], capture_output=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass


def notify_once(st: dict, kind: str, title: str, body: str) -> None:
    now = time.time()
    last = st.get("lastNotify", {}).get(kind)
    if last and now - last < NOTIFY_DEDUPE_S:
        return
    st.setdefault("lastNotify", {})[kind] = now
    notify(title, body)
