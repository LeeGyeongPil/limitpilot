"""Identifiers of the previous tool, claude-account-pilot (the author's earlier project).

Only this module and ``migrate.py`` may use these names. They are needed to find old
data, stop the old daemon and move credentials over (``limitpilot migrate``).
"""

import getpass
import os
import subprocess

from limitpilot import paths

TOOL_NAME = "claude-account-pilot"
STATE_DIRNAME = ".claude-autoswap"
KEYCHAIN_SERVICE = "claude-autoswap"
LOG_NAME = "autoswap.log"
DAEMON_SCRIPT = "claude_autoswap.py"
DASHBOARD_PORT = 8737
DASHBOARD_HEADER = "X-Autoswap"
WIN_TASK_NAME = "ClaudeAccountPilot-AutoSwap"
APP_BUNDLE = "Claude Usage.app"
MIGRATED_MARKER = "MIGRATED"


def state_dir() -> str:
    """Old state folder: ``<home>/.claude-autoswap`` (home = the Claude Code home)."""
    return os.path.join(paths.HOME, STATE_DIRNAME)


def launchd_label() -> str:
    return f"com.{getpass.getuser()}.claude-autoswap"


def launchd_plists():
    label = launchd_label()
    return (os.path.join(paths.HOME, "Library", "LaunchAgents", label + ".plist"),
            os.path.join(state_dir(), label + ".plist"))


def daemon_script_path() -> str:
    return os.path.join(state_dir(), "daemon", DAEMON_SCRIPT)


def helper_command_suffix(n: int) -> str:
    """Tail of the old apiKeyHelper command for gateway slot ``n``."""
    return f"{STATE_DIRNAME}/daemon/{DAEMON_SCRIPT} gateway-key {n}"


def has_data() -> bool:
    return os.path.isfile(os.path.join(state_dir(), "state.json"))


def already_migrated() -> bool:
    return os.path.exists(os.path.join(state_dir(), MIGRATED_MARKER))


def migration_pending() -> bool:
    """Old data exists, it was not migrated yet and limitpilot has no state of its own."""
    return has_data() and not already_migrated() and not os.path.exists(paths.state_file())


def running_legacy_daemons():
    """Human-readable traces of an old daemon that is still installed (no side effects)."""
    found = [p for p in launchd_plists() if os.path.exists(p)]
    if os.path.exists(daemon_script_path()) and not already_migrated():
        found.append(daemon_script_path())
    if paths.IS_WIN:
        try:
            r = subprocess.run(["schtasks", "/Query", "/TN", WIN_TASK_NAME],
                               capture_output=True, text=True)
            if r.returncode == 0:
                found.append(WIN_TASK_NAME)
        except OSError:
            pass
    return found
