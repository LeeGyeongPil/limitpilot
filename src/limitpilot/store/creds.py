"""Active and backup credentials, whatever the backend.

* active credential (owned by Claude Code): Keychain ``Claude Code-credentials`` on
  macOS, ``~/.claude/.credentials.json`` on Windows,
* backup slots (owned by limitpilot): Keychain service ``limitpilot`` / account
  ``slot-N`` on macOS, ``~/.limitpilot/slots/slot-N.json`` on Windows,
* practice mode / tests: the fake JSON store for both (store/fake.py).

Every slot write is read back and compared before it counts as done.
"""

import time

from limitpilot.constants import KC_WRITE_ATTEMPTS, KC_WRITE_RETRY_S
from limitpilot.errors import KeychainError
from limitpilot import i18n
from limitpilot import paths
from limitpilot.store import fake
from limitpilot.store import filestore
from limitpilot.store import keychain


def _use_files() -> bool:
    # 가짜 저장소가 켜져 있으면 OS 와 상관없이 키체인 경로(→ fake)로 보낸다
    return paths.IS_WIN and not fake.active()


def kc_read_active():
    if _use_files():
        return filestore._file_read(paths.CC_CREDS_FILE)
    return keychain.kc_read(paths.ACTIVE_SERVICE, paths.KC_USER)


def kc_write_active(blob: str) -> None:
    if _use_files():
        filestore._file_write(paths.CC_CREDS_FILE, blob)
        return
    keychain.kc_write(paths.ACTIVE_SERVICE, paths.KC_USER, blob)


def kc_read_slot(n: int):
    if _use_files():
        return filestore._file_read(filestore._slot_file(n))
    return keychain.kc_read(paths.BACKUP_SERVICE, paths.backup_account(n))


def kc_write_slot(n: int, blob: str) -> None:
    if _use_files():
        filestore._file_write(filestore._slot_file(n), blob)
    else:
        keychain.kc_write(paths.BACKUP_SERVICE, paths.backup_account(n), blob)
    back = kc_read_slot(n)
    if back != blob:
        raise KeychainError(i18n.t("creds_backup_verify", n=n))


def kc_write_slot_retry(n: int, blob: str,
                        attempts: int = KC_WRITE_ATTEMPTS,
                        delay: float = KC_WRITE_RETRY_S) -> None:
    """kc_write_slot with retries — for a rotated one-time refresh token that exists
    nowhere else, so a momentary Keychain lock does not lose it."""
    for attempt in range(attempts):
        try:
            kc_write_slot(n, blob)
            return
        except KeychainError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(delay)
