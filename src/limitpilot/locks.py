"""Locks: Claude Code's lock-directory convention, the state lock and per-slot locks."""

import os
import random
import sys
import threading
import time
from contextlib import contextmanager

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from limitpilot.constants import (LOCK_STALENESS_S, LOCK_TIMEOUT_S, LOCK_TOUCH_S)
from limitpilot.errors import (LimitPilotError, SwitchError)
from limitpilot import i18n
from limitpilot import paths


# ------------------------------------------- Claude Code 와 같은 락 디렉터리 규약


@contextmanager
def proper_lockfile(lock_dir: str, timeout: float = LOCK_TIMEOUT_S):
    """mkdir mutex compatible with the lock directories Claude Code uses.

    The lock is a directory. While held, its mtime is touched periodically to prove the
    holder is alive; a lock older than the staleness limit is treated as abandoned.
    """
    deadline = time.monotonic() + timeout
    while True:
        # 데드라인은 분기와 무관하게 매 반복 최상단에서 평가 — stale-rmdir 실패가
        # 반복돼도 무한 스핀 없이 반드시 타임아웃된다
        if time.monotonic() > deadline:
            raise SwitchError(i18n.t("lock_failed", path=lock_dir))
        try:
            os.mkdir(lock_dir)
            break
        except FileExistsError:
            pass
        try:
            held_mtime = os.stat(lock_dir).st_mtime
        except FileNotFoundError:
            continue  # 보유자가 mkdir/stat 사이에 해제 — 즉시 재시도
        if time.time() - held_mtime > LOCK_STALENESS_S:
            try:
                os.rmdir(lock_dir)
            except OSError:
                time.sleep(0.05)  # 제거 불가 stale 락 — 핫스핀 방지
            continue
        time.sleep(0.25 + random.random() * 0.25)  # 지터 — Claude Code 대기자와 경합 완화

    stop = threading.Event()

    def _touch():
        while not stop.wait(LOCK_TOUCH_S):
            try:
                os.utime(lock_dir, None)
            except OSError:
                pass

    toucher = threading.Thread(target=_touch, daemon=True)
    toucher.start()
    try:
        yield
    finally:
        stop.set()
        toucher.join(timeout=1.0)
        try:
            os.rmdir(lock_dir)
        except OSError:
            pass


# ----------------------------------------------------------------- FileLock


def _lock_try(fd) -> bool:
    """비차단 배타 락 시도 — 성공 True. (win: msvcrt 바이트범위 / posix: flock)"""
    try:
        if paths.IS_WIN:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, PermissionError, OSError):
        return False


def _lock_release(fd) -> None:
    try:
        if paths.IS_WIN:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def _file_lock(path: str, timeout: float, busy_message: str):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.time() + timeout
    try:
        if paths.IS_WIN:
            os.lseek(fd, 0, os.SEEK_SET)
        while not _lock_try(fd):
            if time.time() > deadline:
                raise LimitPilotError(busy_message)
            time.sleep(0.2)
        yield
    finally:
        try:
            _lock_release(fd)
        finally:
            os.close(fd)


@contextmanager
def state_lock(timeout: float = 30.0):
    """Process-wide lock for reading-modifying-writing ``state.json``.

    Held only for local work (load, decide, switch, save) — never during network calls,
    so the dashboard and CLI do not wait behind a slow usage request.
    """
    with _file_lock(paths.state_lock_file(), timeout,
                    i18n.t("lock_busy")):
        yield


@contextmanager
def slot_lock(n: int, timeout: float = 30.0):
    """Per-slot lock around 'read token → refresh → write rotated token'.

    The refresh token rotates on every refresh, so two processes refreshing the same
    slot at once would make one of them write a dead token. This lock serialises that
    without holding the global state lock during the network call.
    """
    with _file_lock(os.path.join(paths.STATE_DIR, f".slot-{int(n)}.lock"), timeout,
                    i18n.t("lock_slot_busy", n=n)):
        yield


def state_lock_is_free() -> bool:
    """True when nobody holds the state lock right now (diagnostics and tests)."""
    os.makedirs(paths.STATE_DIR, mode=0o700, exist_ok=True)
    fd = os.open(paths.state_lock_file(), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if _lock_try(fd):
            _lock_release(fd)
            return True
        return False
    finally:
        os.close(fd)
