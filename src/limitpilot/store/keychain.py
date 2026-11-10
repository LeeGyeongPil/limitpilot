"""macOS Keychain through /usr/bin/security.

Secrets are passed to `security -i` on stdin as hex, never on the command line (where
`ps` could see them). In practice mode / tests the fake store (store/fake.py) is used instead.
"""

import subprocess
import time

from limitpilot.constants import (KC_NOT_FOUND_RC, KC_READ_ATTEMPTS, KC_READ_RETRY_S, KC_STDIN_LIMIT, KC_TIMEOUT_S)
from limitpilot.errors import (KeychainError)
from limitpilot import i18n
from limitpilot import paths
from limitpilot.store import fake


# ----------------------------------------------------------------- keychain


def _kc_quote(value: str) -> str:
    # security -i는 각 줄을 셸 스타일로 재파싱 — 공백 포함 값은 따옴표+이스케이프 필수
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def kc_read(service: str, account: str):
    """키체인 값 읽기. 없으면 None, 그 외 실패는 KeychainError."""
    if fake.active():
        return fake.read(service, account)
    last_err = None
    for attempt in range(KC_READ_ATTEMPTS):
        try:
            result = subprocess.run(
                [paths.SECURITY, "find-generic-password", "-a", account, "-w", "-s", service],
                capture_output=True, text=True, timeout=KC_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as e:
            raise KeychainError(i18n.t("kc_read_timeout", s=KC_TIMEOUT_S)) from e
        if result.returncode == 0:
            # -w는 값 뒤에 개행 1개를 붙여 출력 — 정확히 그것만 제거
            return result.stdout.removesuffix("\n")
        if result.returncode == KC_NOT_FOUND_RC:
            return None
        last_err = f"rc={result.returncode}: {result.stderr.strip()}"
        if attempt + 1 < KC_READ_ATTEMPTS:
            time.sleep(KC_READ_RETRY_S)
    raise KeychainError(i18n.t("kc_read_failed", err=last_err))


def kc_write(service: str, account: str, blob: str) -> None:
    """키체인 값 쓰기(-U 갱신). 비밀은 hex로 stdin 전달 — argv(ps) 노출 금지."""
    if fake.active():
        fake.write(service, account, blob)
        return
    hex_value = blob.encode("utf-8").hex()
    command = (
        f"add-generic-password -U -a {_kc_quote(account)} -s {_kc_quote(service)} "
        f"-X {hex_value}\n"
    )
    if len(command.encode("utf-8")) > KC_STDIN_LIMIT:
        # argv 폴백은 ps에 토큰이 노출되므로 채택하지 않음 — 현재 blob(~1KB)에선 도달 불가
        raise KeychainError(i18n.t("kc_too_long"))
    try:
        result = subprocess.run(
            [paths.SECURITY, "-i"], input=command,
            capture_output=True, text=True, timeout=KC_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        raise KeychainError(i18n.t("kc_write_timeout", s=KC_TIMEOUT_S)) from e
    if result.returncode != 0:
        raise KeychainError(i18n.t("kc_write_failed", rc=result.returncode, err=result.stderr.strip()))


def kc_delete(service: str, account=None, attempts: int = 3) -> bool:
    """정확한 Keychain generic-password 항목을 삭제한다.

    삭제했으면 True, 이미 없으면 False를 반환한다. 비밀은 argv나
    오류 메시지에 싣지 않는다.
    """
    if fake.active():
        return fake.delete(service, account)
    command = [paths.SECURITY, "delete-generic-password"]
    if account is not None:
        command += ["-a", account]
    command += ["-s", service]
    last_error = None
    for attempt in range(attempts):
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=KC_TIMEOUT_S,
            )
            if result.returncode == 0:
                return True
            if result.returncode == KC_NOT_FOUND_RC:
                return False
            last_error = f"rc={result.returncode}"
        except subprocess.TimeoutExpired:
            last_error = i18n.t("timeout_s", s=KC_TIMEOUT_S)
        except OSError as e:
            last_error = type(e).__name__
        if attempt + 1 < attempts:
            time.sleep(0.2)
    raise KeychainError(i18n.t("kc_delete_failed", err=last_error))
