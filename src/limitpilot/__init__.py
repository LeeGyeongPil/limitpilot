"""LimitPilot — see Claude Code usage per account and switch accounts before a limit hits.

The package uses only the Python standard library. ``__version__`` below is the single
source of the version number (pyproject, the menu bar app's Info.plist, the User-Agent
header and the dashboard health response all read it).
"""

import os as _os

__version__ = "0.2.0"

# LIMITPILOT_SANDBOX=1 (tests, --demo) 이면 실제 키체인·launchctl·외부 네트워크 호출을
# 프로세스 전체에서 막는다. 하위 프로세스도 환경변수를 물려받아 같은 가드를 켠다.
if _os.environ.get("LIMITPILOT_SANDBOX") == "1":
    from limitpilot import sandbox as _sandbox

    _sandbox.install()
