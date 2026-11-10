"""Test-suite guard (runs before any test module is imported).

* puts ``src`` on ``sys.path`` (no install step, no ``.venv``),
* turns on the sandbox: real ``security``/``launchctl``/``schtasks``/``osascript``/``claude``
  calls, non-loopback sockets and writes under the real ``~/.claude*``/``~/.limitpilot``
  raise ``SandboxViolation``,
* points every limitpilot and Claude Code path at a throw-away temp folder,
* fails the whole run at exit if any violation happened outside ``sandbox.expecting()``.
"""

import atexit
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

sys.dont_write_bytecode = True
_SCRATCH = tempfile.mkdtemp(prefix="limitpilot-tests-")
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["LIMITPILOT_SANDBOX"] = "1"
os.environ["LIMITPILOT_HOME"] = os.path.join(_SCRATCH, "state")
os.environ["LIMITPILOT_CLAUDE_HOME"] = os.path.join(_SCRATCH, "claude-home")
# 이식한 테스트는 한국어 문구를 단언한다 — 언어를 고정해 OS 로케일과 무관하게 만든다.
os.environ["LIMITPILOT_LANG"] = "ko"
os.makedirs(os.environ["LIMITPILOT_CLAUDE_HOME"], exist_ok=True)

import limitpilot  # noqa: E402  (installs the sandbox hook)
from limitpilot import sandbox  # noqa: E402

assert sandbox.installed(), "sandbox hook must be active for the test suite"


def _finish() -> None:
    shutil.rmtree(_SCRATCH, ignore_errors=True)
    bad = sandbox.unexpected()
    if bad:
        sys.stderr.write("SANDBOX VIOLATIONS during tests:\n")
        for kind, detail in bad:
            sys.stderr.write(f"  {kind}: {detail}\n")
        sys.stderr.flush()
        os._exit(1)


atexit.register(_finish)
