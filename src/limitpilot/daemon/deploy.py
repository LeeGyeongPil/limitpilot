"""The daemon's own copy of limitpilot: ``~/.limitpilot/daemon/limitpilot.pyz``.

launchd / Task Scheduler run this single zipapp file every 60 seconds, so the daemon keeps
working no matter where (or whether) the package that installed it still lives — a pipx
venv can move, a git clone can be deleted.

The archive is built deterministically (sorted entries, fixed timestamps) and carries a
fingerprint of the package sources in its zip comment. Comparing fingerprints is cheap,
so every CLI run can check whether the daemon copy is out of date.
"""

import hashlib
import io
import os
import re
import sys
import tempfile
import zipfile

from limitpilot import __version__ as APP_VERSION
from limitpilot.errors import LimitPilotError
from limitpilot.daemon import launchd
from limitpilot.daemon import schtasks
from limitpilot import i18n
from limitpilot import logutil
from limitpilot import paths

PYZ_NAME = "limitpilot.pyz"
_FIXED_TIME = (2020, 1, 1, 0, 0, 0)  # 같은 소스 → 같은 바이트(결정적 아카이브)
_COMMENT_PREFIX = b"limitpilot-fingerprint:"
_VERSION_RE = re.compile(rb'^__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', re.M)

# pyz 의 진입점 — `python limitpilot.pyz auto --once`
_PYZ_MAIN = (
    "import sys\n"
    "from limitpilot.cli import main\n"
    "sys.exit(main())\n"
)


def find_python() -> str:
    # install-daemon 을 실행한 인터프리터를 그대로 쓴다. launchd 의 최소 PATH 에서
    # `python3` 를 찾으면 다른(예: 오래된 시스템) 인터프리터로 풀릴 수 있다.
    if paths.IS_WIN and sys.executable:
        # 콘솔 창 깜빡임 방지 — 같은 위치의 pythonw.exe 가 있으면 우선
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(pythonw):
            return pythonw
    if sys.executable and os.path.exists(sys.executable):
        return sys.executable
    return "/usr/bin/python3"


def package_root() -> str:
    """Folder of the running ``limitpilot`` package (may be inside a .pyz)."""
    import limitpilot

    return os.path.dirname(os.path.abspath(limitpilot.__file__))


def running_archive():
    """Path of the .pyz we are running from, or None for a normal install."""
    root = package_root()
    archive = os.path.dirname(root)
    if os.path.isfile(archive) and zipfile.is_zipfile(archive):
        return archive
    return None


def _package_files(root: str):
    """(arcname, bytes) for every source/data file of the package, sorted."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if name.endswith((".pyc", ".pyo")) or name == ".DS_Store":
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, os.path.dirname(root)).replace(os.sep, "/")
            with open(full, "rb") as handle:
                out.append((rel, handle.read()))
    return sorted(out)


def source_fingerprint(files=None) -> str:
    digest = hashlib.sha256()
    for rel, data in files if files is not None else _package_files(package_root()):
        digest.update(rel.encode() + b"\0" + data + b"\0")
    return digest.hexdigest()[:20]


def build_pyz_bytes() -> bytes:
    """Build the zipapp in memory (deterministic)."""
    archive = running_archive()
    if archive:
        # pyz 안에서 실행 중이면 그 파일이 곧 배포본이다.
        with open(archive, "rb") as handle:
            return handle.read()
    files = _package_files(package_root())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, data in [("__main__.py", _PYZ_MAIN.encode())] + files:
            info = zipfile.ZipInfo(rel, date_time=_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
        zf.comment = _COMMENT_PREFIX + source_fingerprint(files).encode()
    return buf.getvalue()


def write_pyz(target: str) -> str:
    """Atomically write the zipapp to ``target`` (mode 0600 file, 0700 folder)."""
    target_dir = os.path.dirname(os.path.abspath(target))
    os.makedirs(target_dir, mode=0o700, exist_ok=True)
    data = build_pyz_bytes()
    fd, tmp = tempfile.mkstemp(prefix=".limitpilot-", suffix=".tmp", dir=target_dir)
    try:
        with os.fdopen(fd, "wb") as dst:
            dst.write(data)
            dst.flush()
            os.fsync(dst.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def deployed_daemon_script_path() -> str:
    """Daemon copy path, independent of where the package is installed."""
    return os.path.join(paths.daemon_dir(), PYZ_NAME)


def deploy_daemon_script(source_path=None) -> str:
    """Write the daemon copy under the state folder and return its path.

    ``source_path`` (optional) is an existing .pyz to copy instead of building one.
    """
    target = deployed_daemon_script_path()
    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
    try:
        os.chmod(os.path.dirname(target), 0o700)
    except OSError:
        pass
    if source_path:
        fd, tmp = tempfile.mkstemp(prefix=".limitpilot-", suffix=".tmp",
                                   dir=os.path.dirname(target))
        try:
            with os.fdopen(fd, "wb") as dst, open(source_path, "rb") as src:
                dst.write(src.read())
                dst.flush()
                os.fsync(dst.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return target
    return write_pyz(target)


def version_tuple(v):
    """'1.2.3' → (1, 2, 3). None if it cannot be parsed."""
    m = re.fullmatch(r"\s*(\d+)\.(\d+)\.(\d+)\s*", str(v or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def script_app_version(path: str):
    """Version tuple stored in a deployed .pyz, or None when unreadable."""
    try:
        with zipfile.ZipFile(path) as zf:
            data = zf.read("limitpilot/__init__.py")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    m = _VERSION_RE.search(data)
    return version_tuple(m.group(1).decode()) if m else None


def deployed_fingerprint(path: str):
    try:
        with zipfile.ZipFile(path) as zf:
            comment = zf.comment
    except (OSError, zipfile.BadZipFile):
        return None
    if comment.startswith(_COMMENT_PREFIX):
        return comment[len(_COMMENT_PREFIX):].decode("ascii", "replace")
    return None


def maybe_refresh_daemon_copy() -> None:
    """Refresh the daemon copy when this code differs from it.

    launchd runs the copy's path, so the next tick picks up new code without
    re-registering. A copy with a *higher* version is never overwritten, so running an
    old install cannot downgrade the daemon. Running from the copy itself is a no-op.
    """
    target = deployed_daemon_script_path()
    if not os.path.exists(target):
        return
    archive = running_archive()
    if archive and os.path.abspath(archive) == os.path.abspath(target):
        return
    try:
        mine = deployed_fingerprint(archive) if archive else source_fingerprint()
        if mine and mine == deployed_fingerprint(target):
            return
        deployed = script_app_version(target)
        if deployed and deployed > version_tuple(APP_VERSION):
            logutil.log(i18n.t("dep_newer", deployed=".".join(map(str, deployed)), mine=APP_VERSION))
            return
        deploy_daemon_script(archive)
        logutil.log(i18n.t("dep_refreshed"))
    except Exception as e:  # noqa: BLE001 — 갱신 실패가 CLI 명령을 막으면 안 된다
        logutil.log(i18n.t("dep_refresh_failed", err=e))


def _remove_deployed_daemon(strict: bool = False) -> None:
    target = deployed_daemon_script_path()
    try:
        os.remove(target)
    except FileNotFoundError:
        pass
    except OSError as e:
        if strict:
            raise LimitPilotError(i18n.t("dep_remove_failed")) from e
    try:
        os.rmdir(os.path.dirname(target))
    except OSError:
        pass
    if strict and os.path.lexists(target):
        raise LimitPilotError(i18n.t("dep_left"))


def _stop_daemon(strict: bool = False) -> None:
    if paths.IS_WIN:
        schtasks._win_uninstall_daemon(strict=strict)
    else:
        launchd._mac_uninstall_daemon(strict=strict)
    _remove_deployed_daemon(strict=strict)
