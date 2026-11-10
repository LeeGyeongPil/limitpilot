"""File backend (Windows credential files) and atomic JSON writes.

On macOS Claude Code keeps credentials in the Keychain; on Windows it keeps them in
``%USERPROFILE%\\.claude\\.credentials.json``, protected by the user-profile ACLs
(Claude Code docs, "Credential management": https://code.claude.com/docs/en/iam, checked
2026-11-10). limitpilot's Windows backup slots use files with the same protection level
under ``~/.limitpilot/slots/``.
"""

import json
import os
import tempfile

from limitpilot.errors import KeychainError
from limitpilot import i18n
from limitpilot import paths


def _slot_file(n: int) -> str:
    return os.path.join(paths.STATE_DIR, "slots", f"slot-{n}.json")


def _file_read(path: str):
    """자격증명 파일 읽기 — 없으면 None(키체인 not-found와 동일 계약)."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise KeychainError(i18n.t("fs_read_failed", err=e)) from e


def _file_write(path: str, blob: str) -> None:
    """자격증명 파일 원자적 쓰기(mkstemp + replace)."""
    d = os.path.dirname(path)
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".cred-", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            if not paths.IS_WIN:
                os.chmod(tmp, 0o600)  # Windows는 NTFS ACL(사용자 프로필) 의존
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        raise KeychainError(i18n.t("fs_write_failed", err=e)) from e


# ------------------------------------------------------------------ json io


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def atomic_write_json(path: str, obj: dict, mode=None) -> None:
    """Write JSON atomically: unique temp file (mkstemp, 0600 from the start) + replace.

    An existing file keeps its permission bits (``~/.claude.json`` and
    ``~/.claude/settings.json`` belong to Claude Code — we must not change their mode).
    New files get ``mode`` (default 0600).
    """
    data = json.dumps(obj, ensure_ascii=False, indent=2)
    json.loads(data)  # 직렬화 결과를 다시 읽어 본 뒤에만 원본을 바꾼다
    try:
        keep = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        keep = 0o600 if mode is None else mode
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".limitpilot-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if not paths.IS_WIN:
            os.chmod(tmp, keep)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
