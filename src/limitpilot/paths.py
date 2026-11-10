"""Names and filesystem locations.

Two kinds of names live here:

* limitpilot's own names (state folder, keychain service, launchd label, ...).
* Names owned by Claude Code (``Claude Code-credentials``, ``~/.claude.json``,
  ``~/.claude/settings.json``, ...). These must match Claude Code exactly and are never
  renamed.

Other modules read these as ``paths.NAME`` at call time, so tests can patch one value
here and every caller sees it. ``LIMITPILOT_HOME`` and ``LIMITPILOT_CLAUDE_HOME`` move
the state folder and the Claude Code files to another folder (tests, ``--demo``).
"""

import getpass
import os
import sys
import tempfile

# sys.platform 를 쓴다 — platform.system() 은 Windows 에서 WMI 조회로 오래 멈출 수 있다.
IS_WIN = sys.platform == "win32"

# 샌드박스(테스트·연습 모드)에서는 실제 홈을 절대 기본값으로 쓰지 않는다.
_SANDBOX = os.environ.get("LIMITPILOT_SANDBOX") == "1"
_SANDBOX_ROOT = os.path.join(tempfile.gettempdir(), "limitpilot-sandbox")


def _claude_home() -> str:
    """Folder that holds ``.claude.json`` and ``.claude/`` (the user's home normally)."""
    override = os.environ.get("LIMITPILOT_CLAUDE_HOME")
    if override:
        return os.path.abspath(override)
    if _SANDBOX:
        return os.path.join(_SANDBOX_ROOT, "claude-home")
    return os.path.expanduser("~")


def _state_dir(home: str) -> str:
    override = os.environ.get("LIMITPILOT_HOME")
    if override:
        return os.path.abspath(override)
    if _SANDBOX:
        return os.path.join(_SANDBOX_ROOT, "state")
    return os.path.join(home, ".limitpilot")


# ---------------------------------------------------------------- Claude Code 소유 이름

HOME = _claude_home()
KC_USER = getpass.getuser()  # Claude Code 가 키체인 account 로 쓰는 로그인 사용자 이름
SECURITY = "/usr/bin/security"
OSASCRIPT = "/usr/bin/osascript"

# 활성(라이브) 자격증명 — Claude Code 소유
ACTIVE_SERVICE = "Claude Code-credentials"
CLAUDE_KEYCHAIN_BASE = "Claude Code-credentials"
CLAUDE_JSON = os.path.join(HOME, ".claude.json")
CLAUDE_SETTINGS_FILE = os.path.join(HOME, ".claude", "settings.json")
CC_CREDS_FILE = os.path.join(HOME, ".claude", ".credentials.json")  # 있을 때만 mtime 을 갱신

# Claude Code 와 같은 락 디렉터리 규약(디렉터리 mkdir 뮤텍스)
CRED_LOCK_DIR = HOME + "/.claude.lock"
CONF_LOCK_DIR = CLAUDE_JSON + ".lock"

def _claude_bin_candidates(home: str):
    if not IS_WIN:
        return ["/opt/homebrew/bin/claude", "/usr/local/bin/claude",
                os.path.join(home, ".local", "bin", "claude"),
                os.path.join(home, ".claude", "local", "claude")]
    # Windows 의 claude 는 npm 전역 .cmd 심 — shutil.which("claude") 가 PATHEXT 로
    # 먼저 찾고, PATH 에 없을 때만 아래 위치를 확인한다
    return [os.path.expandvars(r"%APPDATA%\npm\claude.cmd"),
            os.path.join(home, ".local", "bin", "claude.exe"),   # 네이티브 설치기 기본 위치
            os.path.join(home, ".claude", "local", "claude.cmd"),
            os.path.join(home, ".claude", "local", "claude.exe")]


_CLAUDE_BIN_CANDIDATES = _claude_bin_candidates(HOME)

# ---------------------------------------------------------------- limitpilot 이름

APP_NAME = "LimitPilot"
# 백업 키체인(슬롯 수 제한 없음 — 슬롯 n ↔ 키체인 account "slot-n")
BACKUP_SERVICE = "limitpilot"


def backup_account(n: int) -> str:
    return f"slot-{n}"


STATE_DIR = _state_dir(HOME)
DASHBOARD_APP_ID = "limitpilot-dashboard"
DASHBOARD_DEFAULT_PORT = int(os.environ.get("LIMITPILOT_PORT") or 8748)
DASHBOARD_RUNTIME_PREFIX = "dashboard-runtime-"
DASHBOARD_HEADER = "X-LimitPilot"

LAUNCHD_LABEL = "io.github.limitpilot.daemon"
LAUNCHD_PLIST = os.path.join(HOME, "Library", "LaunchAgents", LAUNCHD_LABEL + ".plist")
WIN_TASK_NAME = "LimitPilot-Daemon"
BUNDLE_ID = "io.github.limitpilot"


def state_dir() -> str:
    return STATE_DIR


def state_file() -> str:
    return os.path.join(STATE_DIR, "state.json")


def state_lock_file() -> str:
    return os.path.join(STATE_DIR, ".lock")


def log_file() -> str:
    return os.path.join(STATE_DIR, "limitpilot.log")


def claude_home() -> str:
    return HOME


def daemon_dir() -> str:
    return os.path.join(STATE_DIR, "daemon")


def configure() -> None:
    """Re-read LIMITPILOT_HOME / LIMITPILOT_CLAUDE_HOME (``--demo`` calls this after
    pointing them at a temp folder). Every module reads ``paths.X`` at call time."""
    global _SANDBOX, HOME, CLAUDE_JSON, CLAUDE_SETTINGS_FILE, CC_CREDS_FILE, CRED_LOCK_DIR
    global CONF_LOCK_DIR, STATE_DIR, LAUNCHD_PLIST, _CLAUDE_BIN_CANDIDATES, DASHBOARD_DEFAULT_PORT
    _SANDBOX = os.environ.get("LIMITPILOT_SANDBOX") == "1"
    HOME = _claude_home()
    CLAUDE_JSON = os.path.join(HOME, ".claude.json")
    CLAUDE_SETTINGS_FILE = os.path.join(HOME, ".claude", "settings.json")
    CC_CREDS_FILE = os.path.join(HOME, ".claude", ".credentials.json")
    CRED_LOCK_DIR = HOME + "/.claude.lock"
    CONF_LOCK_DIR = CLAUDE_JSON + ".lock"
    STATE_DIR = _state_dir(HOME)
    LAUNCHD_PLIST = os.path.join(HOME, "Library", "LaunchAgents", LAUNCHD_LABEL + ".plist")
    _CLAUDE_BIN_CANDIDATES = _claude_bin_candidates(HOME)
    DASHBOARD_DEFAULT_PORT = int(os.environ.get("LIMITPILOT_PORT") or 8748)
