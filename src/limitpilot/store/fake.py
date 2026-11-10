"""Fake credential store: a JSON file in a temp folder (practice mode and tests).

When active (``activate(path)`` or ``LIMITPILOT_FAKE_STORE=<path>``), every keychain /
credential-file read and write in ``store.keychain`` and ``store.creds`` goes here
instead. Items are keyed by ``(service, account)`` exactly like the macOS Keychain, so the
switching code — write, read back, compare, roll back — runs unchanged.
"""

import os
import threading

from limitpilot.store import filestore

_lock = threading.Lock()
_path = None
CALLS = []          # (op, service, account) — 최근 호출 기록(테스트용), 상한 1,000개
_MAX_CALLS = 1000


def activate(path: str) -> None:
    global _path
    _path = os.path.abspath(path)
    os.environ["LIMITPILOT_FAKE_STORE"] = _path   # 하위 프로세스(대시보드 등)도 같은 저장소


def deactivate() -> None:
    global _path
    _path = None
    os.environ.pop("LIMITPILOT_FAKE_STORE", None)


def active() -> bool:
    return _current() is not None


def _current():
    return _path or os.environ.get("LIMITPILOT_FAKE_STORE") or None


def _key(service: str, account: str) -> str:
    return f"{service}\u0000{account}"


def _load() -> dict:
    try:
        return filestore.read_json(_current())
    except (OSError, ValueError):
        return {}


def _record(op: str, service: str, account) -> None:
    CALLS.append((op, service, account))
    del CALLS[:-_MAX_CALLS]


def read(service: str, account: str):
    with _lock:
        _record("read", service, account)
        return _load().get(_key(service, account))


def write(service: str, account: str, blob: str) -> None:
    with _lock:
        _record("write", service, account)
        data = _load()
        data[_key(service, account)] = blob
        filestore.atomic_write_json(_current(), data)


def delete(service: str, account=None) -> bool:
    """Delete one item, or every item of ``service`` when ``account`` is None."""
    with _lock:
        _record("delete", service, account)
        data = _load()
        if account is None:
            keys = [k for k in data if k.split("\u0000", 1)[0] == service]
        else:
            keys = [k for k in (_key(service, account),) if k in data]
        for k in keys:
            del data[k]
        filestore.atomic_write_json(_current(), data)
        return bool(keys)


def services():
    """All (service, account) pairs (for purge / migrate checks)."""
    with _lock:
        return [tuple(k.split("\u0000", 1)) for k in _load()]
