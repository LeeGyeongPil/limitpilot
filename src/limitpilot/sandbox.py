"""Sandbox switch: block real keychain, daemon, notification and network calls.

When ``LIMITPILOT_SANDBOX=1`` (set automatically by ``--demo`` and by the test suite),
``install()`` registers a Python audit hook (PEP 578). The hook sees every
``subprocess``/``os.system`` start, every socket connect/DNS lookup, every
``urllib`` request and every write under the real Claude Code / limitpilot folders,
and raises :class:`SandboxViolation` instead of letting it happen. Each attempt is
counted so the demo can print ``sandbox: real keychain 0, network 0, launchctl 0``.

Fake executables that tests create in the temp folder are allowed; only the real
system tools are blocked.
"""

import contextlib
import os
import sys
import tempfile
import threading

# 막을 실행 파일 이름 → 집계 분류
_BLOCKED_EXES = {
    "security": "keychain",
    "launchctl": "launchctl",
    "schtasks": "schtasks",
    "osascript": "osascript",
    "powershell": "osascript",
    "claude": "claude",
    "open": "open",
}
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "", None}

_installed = False
_lock = threading.Lock()
_local = threading.local()
_counts = {"keychain": 0, "launchctl": 0, "schtasks": 0, "osascript": 0, "claude": 0,
           "open": 0, "network": 0, "home_write": 0}
_unexpected = []   # (kind, detail) — 테스트 전역 가드가 종료 시 확인한다
_expect_depth = 0


class SandboxViolation(RuntimeError):
    """Raised when sandboxed code tries to touch a real credential store or the network.

    It is deliberately not an ``OSError`` so ``except OSError`` blocks cannot hide it.
    """


def enabled() -> bool:
    return os.environ.get("LIMITPILOT_SANDBOX") == "1"


def _real_home() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, AttributeError):
        return os.path.expanduser("~")


def _protected_roots():
    from limitpilot import legacy   # 이전 도구 폴더도 보호한다(이름은 legacy 에만 둔다)

    home = _real_home()
    return tuple(os.path.join(home, p) for p in (
        ".limitpilot", legacy.STATE_DIRNAME, ".claude", ".claude.json", ".claude.json.lock",
        ".claude.lock", os.path.join("Library", "LaunchAgents"),
    ))


_PROTECTED = None
_TEMP_ROOTS = None


def _under(path: str, roots) -> bool:
    return any(path == r or path.startswith(r + os.sep) for r in roots)


def _norm(path) -> str:
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, str):
        return ""
    return os.path.realpath(os.path.abspath(path))


def _record(kind: str, detail: str) -> None:
    with _lock:
        _counts[kind] = _counts.get(kind, 0) + 1
        if _expect_depth == 0:
            _unexpected.append((kind, detail))
    raise SandboxViolation(f"sandbox blocked {kind}: {detail}")


def _exe_kind(argv0) -> str:
    if not argv0:
        return ""
    path = os.fsdecode(argv0) if isinstance(argv0, bytes) else str(argv0)
    base = os.path.basename(path).lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    kind = _BLOCKED_EXES.get(base)
    if not kind:
        return ""
    # 테스트가 임시 폴더에 만든 가짜 실행 파일은 허용한다.
    if os.sep in path and _under(_norm(path), _TEMP_ROOTS):
        return ""
    return kind


def _first_arg(args):
    if isinstance(args, (list, tuple)) and args:
        return args[0]
    if isinstance(args, (str, bytes)):
        text = os.fsdecode(args) if isinstance(args, bytes) else args
        return text.split()[0] if text.split() else ""
    return ""


def _is_write_open(mode, flags) -> bool:
    if isinstance(mode, str) and any(c in mode for c in "wax+"):
        return True
    if isinstance(flags, int):
        return bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
    return False


def _hook(event: str, args) -> None:
    if getattr(_local, "busy", False):
        return
    if event not in _WATCHED:
        return
    _local.busy = True
    try:
        _check(event, args)
    finally:
        _local.busy = False


def _check(event: str, args) -> None:
    if event == "subprocess.Popen":
        executable, argv = args[0], args[1]
        kind = _exe_kind(executable) or _exe_kind(_first_arg(argv))
        if kind:
            _record(kind, str(_first_arg(argv) or executable))
    elif event in ("os.system", "os.posix_spawn", "os.exec", "os.spawn"):
        cmd = args[0] if event == "os.system" else args[0]
        kind = _exe_kind(_first_arg(cmd) if event == "os.system" else cmd)
        if kind:
            _record(kind, str(cmd))
    elif event == "socket.connect":
        address = args[1]
        if isinstance(address, tuple) and address and address[0] not in _LOOPBACK:
            _record("network", f"connect {address[0]}")
    elif event == "socket.getaddrinfo":
        host = args[0]
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        if host not in _LOOPBACK:
            _record("network", f"dns {host}")
    elif event == "urllib.Request":
        from urllib.parse import urlsplit

        host = urlsplit(args[0]).hostname
        if host not in _LOOPBACK:
            _record("network", f"request {host}")
    elif event == "open":
        path, mode, flags = args[0], args[1], args[2]
        if _is_write_open(mode, flags):
            real = _norm(path)
            if real and _under(real, _PROTECTED):
                _record("home_write", real)
    elif event in ("os.mkdir", "os.remove", "os.rmdir", "os.rename", "shutil.rmtree",
                   "os.chmod", "os.utime", "os.symlink", "os.truncate"):
        for candidate in args[:2] if event in ("os.rename", "os.symlink") else args[:1]:
            real = _norm(candidate)
            if real and _under(real, _PROTECTED):
                _record("home_write", real)


_WATCHED = frozenset({
    "subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.spawn",
    "socket.connect", "socket.getaddrinfo", "urllib.Request", "open",
    "os.mkdir", "os.remove", "os.rmdir", "os.rename", "shutil.rmtree", "os.chmod",
    "os.utime", "os.symlink", "os.truncate",
})


def install() -> None:
    """Register the audit hook once per process (hooks cannot be removed)."""
    global _installed, _PROTECTED, _TEMP_ROOTS
    with _lock:
        if _installed:
            return
        _PROTECTED = tuple(_norm(p) for p in _protected_roots())
        temp = {_norm(tempfile.gettempdir()), _norm("/private/tmp"), _norm("/tmp")}
        _TEMP_ROOTS = tuple(t for t in temp if t)
        _installed = True
    sys.addaudithook(_hook)


def installed() -> bool:
    return _installed


def counts() -> dict:
    with _lock:
        return dict(_counts)


def unexpected():
    with _lock:
        return list(_unexpected)


@contextlib.contextmanager
def expecting():
    """Violations inside this block are counted but not reported as unexpected."""
    global _expect_depth
    with _lock:
        _expect_depth += 1
    try:
        yield
    finally:
        with _lock:
            _expect_depth -= 1


def summary_line() -> str:
    c = counts()
    return (f"sandbox: real keychain {c['keychain']}, network {c['network']}, "
            f"launchctl {c['launchctl'] + c['schtasks']}")
