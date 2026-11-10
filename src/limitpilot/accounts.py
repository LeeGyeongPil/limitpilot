"""Account registry: which slot is which account, registering, removing, enabling.

Identity is the (account UUID, organization UUID) pair from Claude Code's
``~/.claude.json`` ``oauthAccount``; e-mail is only a fallback for display.
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone

from limitpilot.i18n import t as _t
from limitpilot.constants import KC_NOT_FOUND_RC, KC_TIMEOUT_S
from limitpilot.errors import LimitPilotError, KeychainError
from limitpilot import i18n
from limitpilot import gateway
from limitpilot.login import session
from limitpilot import logutil
from limitpilot import paths
from limitpilot.policy import limits
from limitpilot import state
from limitpilot.store import creds
from limitpilot.store import filestore

# 사용자가 계정마다 직접 정한 값 — 같은 계정을 다시 등록(add·login)해도 그대로 둔다.
# 별칭(set_slot_alias), 세션·주간 임계와 주간 최대치(policy/limits.set_slot_limits·set_slot_weekly_cap).
USER_SLOT_KEYS = ("alias", "sessionThreshold", "weeklyThreshold", "weeklyCap")


def _keep_user_settings(prev: dict, slot: dict) -> dict:
    """Copy the user's per-account choices from the old slot record into the new one.

    Re-registering only refreshes the credential and the identity; it must never quietly
    reset a weekly maximum (a safety limit) or an alias the user picked. A value the
    caller set explicitly (e.g. ``login --alias``) wins over the old one.
    """
    for key in USER_SLOT_KEYS:
        if slot.get(key) is None and prev.get(key) is not None:
            slot[key] = prev[key]
    return slot


def identity_uuid_pair(oauth_account: dict):
    """계정 식별의 진실인 (account UUID, organization UUID) 쌍을 반환한다."""
    oauth_account = oauth_account or {}
    account_uuid = oauth_account.get("accountUuid")
    organization_uuid = oauth_account.get("organizationUuid")
    if not isinstance(account_uuid, str) or not account_uuid:
        return None
    if not isinstance(organization_uuid, str) or not organization_uuid:
        return None
    return account_uuid, organization_uuid


def read_live_oauth_account():
    """기본 Claude 프로필의 현재 계정 식별자를 읽는다. 실패/불명이면 None.

    자격증명은 읽지 않으며, 호출 자체는 상태/Keychain을 변경하지 않는다.
    """
    try:
        oauth_account = filestore.read_json(paths.CLAUDE_JSON).get("oauthAccount")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return oauth_account if isinstance(oauth_account, dict) else None


def find_slot_by_uuid(st: dict, oauth_account: dict):
    """이메일 추정 없이 UUID 쌍이 정확히 같은 등록 슬롯만 찾는다."""
    pair = identity_uuid_pair(oauth_account)
    if pair is None:
        return None
    for n in state.registered_slots(st):
        current = (state.slot_of(st, n) or {}).get("oauthAccount") or {}
        if identity_uuid_pair(current) == pair:
            return n
    return None


def inspect_live_account(st: dict) -> dict:
    """실제 기본 Claude 계정과 등록 슬롯의 결합 상태를 읽기 전용으로 판별한다.

    반환 status는 managed/unmanaged/unknown이다. 이메일이나 UUID 자체는 반환하지
    않아 UI/로그에서 계정 식별자가 우발적으로 노출되지 않는다.
    """
    state_slot = st.get("activeSlot")
    current = state.slot_of(st, state_slot) if isinstance(state_slot, int) else None
    # Gateway routing lives in settings.json while the default OAuth credential
    # intentionally remains in Keychain. Find the active gateway from those
    # routing settings before looking at the underlying OAuth identity. This also
    # repairs a stale stateSlot that an older dashboard poll may have overwritten.
    for gateway_slot in state.registered_slots(st):
        if (limits.is_gateway_slot(state.slot_of(st, gateway_slot))
                and gateway.gateway_config_is_active(st, gateway_slot)):
            return {
                "status": "managed", "slot": gateway_slot, "stateSlot": state_slot,
                "changed": state_slot != gateway_slot,
            }
    if limits.is_gateway_slot(current):
        # A partial/manual settings edit must not silently reclassify the underlying
        # OAuth credential while gateway routing keys may still be present.
        if st.get("gatewayRestore") is not None:
            return {
                "status": "unknown", "slot": None, "stateSlot": state_slot,
                "changed": False,
            }
    elif st.get("gatewayRestore") is not None:
        # A stale stateSlot plus an incomplete gateway settings write is ambiguous;
        # never let the still-present OAuth credential silently win in this window.
        return {
            "status": "unknown", "slot": None, "stateSlot": state_slot,
            "changed": False,
        }
    live_oauth = read_live_oauth_account()
    pair = identity_uuid_pair(live_oauth)
    if pair is None:
        return {
            "status": "unknown", "slot": None, "stateSlot": state_slot,
            "changed": False,
        }
    live_slot = find_slot_by_uuid(st, live_oauth)
    if live_slot is None:
        return {
            "status": "unmanaged", "slot": None, "stateSlot": state_slot,
            "changed": state_slot is not None,
        }
    return {
        "status": "managed", "slot": live_slot, "stateSlot": state_slot,
        "changed": state_slot != live_slot,
    }


def reconcile_active_slot(st: dict) -> dict:
    """실제 UUID 결합에 맞춰 메모리의 activeSlot을 보정한다.

    파일/Keychain에는 쓰지 않는다. 영속화할 호출자는 state_lock을 보유한 채
    save_state를 별도로 호출해야 한다. live 상태를 읽지 못한 unknown에서는 기존
    값을 보존하고, 유효하지만 미등록인 계정이면 activeSlot을 None으로 바꾼다.
    """
    binding = inspect_live_account(st)
    if binding["status"] == "managed":
        if binding["slot"] != st.get("activeSlot"):
            # 도구 밖에서 바뀐 활성(사용자의 /login 등) — 사용자의 선택이므로
            # 리셋 임박 선제 이동이 곧바로 덮어쓰지 않게 표시한다
            st["manualChoiceAt"] = time.time()
        st["activeSlot"] = binding["slot"]
    elif binding["status"] == "unmanaged":
        st["activeSlot"] = None
    return binding


def find_slot_by_identity(st: dict, oauth_account: dict):
    """계정/조직 UUID 우선, 구형 슬롯은 이메일로 동일 계정을 찾는다."""
    oauth_account = oauth_account or {}
    uuid_slot = find_slot_by_uuid(st, oauth_account)
    if uuid_slot is not None:
        return uuid_slot
    email = oauth_account.get("emailAddress")
    if email:
        for n in state.registered_slots(st):
            current_slot = state.slot_of(st, n) or {}
            current = current_slot.get("oauthAccount") or {}
            # UUID가 모두 있는 현대 슬롯끼리는 위의 UUID 쌍만 진실이다. 이메일
            # fallback은 UUID가 없던 구형 슬롯을 마이그레이션할 때만 허용한다.
            if current.get("accountUuid") and current.get("organizationUuid"):
                continue
            current_email = current_slot.get("email")
            if isinstance(current_email, str) and current_email.casefold() == email.casefold():
                return n
    return None


def register_oauth_slot(st: dict, blob: str, oauth_account: dict, alias=None):
    """정식 OAuth credentials를 백업 슬롯에 등록한다. 활성 credentials는 변경하지 않는다.
    반환 (slot_n, label, is_new). 호출자가 state_lock을 보유해야 한다."""
    session.validate_login_credentials(blob, oauth_account)
    identity = state.extract_identity(oauth_account)
    email = identity["emailAddress"]
    alias = (alias or "").strip() or None
    live_pair = identity_uuid_pair(read_live_oauth_account())
    identity_pair = identity_uuid_pair(identity)
    live_is_this_account = live_pair is not None and live_pair == identity_pair
    n = find_slot_by_identity(st, identity)
    if n is not None and st.get("activeSlot") == n:
        raise LimitPilotError(_t("already_active_account"))
    is_new = n is None
    if n is None:
        n = state.next_free_slot(st)
    prev = state.slot_of(st, n) or {}
    creds.kc_write_slot(n, blob)
    st["slots"][str(n)] = _keep_user_settings(prev, {
        "alias": alias,   # None 이면 아래 _keep_user_settings 가 예전 별칭을 되살린다
        "email": email,
        "orgName": identity.get("organizationName") or prev.get("orgName"),
        "addedAt": prev.get("addedAt") or datetime.now(timezone.utc).isoformat(),
        "oauthAccount": identity,
        "usage": prev.get("usage"),
        "usageFetchedAt": prev.get("usageFetchedAt"),
        "quarantine": None,
        "enabled": True,
        "kind": "oauth",
    })
    # 격리 로그인은 기본 credentials를 바꾸지 않는다. 다만 사용자가 별도로
    # 기본 Claude에서도 같은 UUID 계정에 로그인한 상태라면 표시 상태만 실제와
    # 맞춘다. 활성 Keychain에는 절대 쓰지 않는다.
    if live_is_this_account:
        if st.get("activeSlot") != n:
            st["manualChoiceAt"] = time.time()  # 사용자가 직접 로그인한 계정 — 선제 이동 유예
        st["activeSlot"] = n
    state.save_state(st)
    return n, state.slot_label(st["slots"][str(n)], n), is_new


def delete_slot(st: dict, n: int) -> str:
    """슬롯 등록 해제(state + 키체인 백업). 호출자가 state_lock 보유.
    활성 슬롯은 거부 — 먼저 다른 계정으로 전환해야 한다."""
    reconcile_active_slot(st)
    s = state.slot_of(st, n)
    if not s:
        raise LimitPilotError(_t("slot_not_registered", n=n))
    if st.get("activeSlot") == n:
        raise LimitPilotError(_t("cannot_remove_active"))
    email = s.get("email")
    if paths.IS_WIN:
        try:
            os.remove(filestore._slot_file(n))
        except FileNotFoundError:
            pass
        except OSError as e:
            raise KeychainError(_t("backup_file_delete_failed", n=n)) from e
    else:
        try:
            result = subprocess.run(
                [paths.SECURITY, "delete-generic-password", "-a", paths.backup_account(n),
                 "-s", paths.BACKUP_SERVICE],
                capture_output=True, text=True, timeout=KC_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            raise KeychainError(_t("keychain_delete_failed", n=n)) from e
        if result.returncode not in (0, KC_NOT_FOUND_RC):
            raise KeychainError(
                _t("keychain_delete_failed_rc", n=n, rc=result.returncode)
            )
    st["slots"].pop(str(n), None)
    state.save_state(st)
    logutil.log(_t("slot_removed", n=n, email=email))
    return email


def set_slot_enabled(st: dict, n: int, enabled: bool) -> str:
    """슬롯 on/off. OFF는 자동전환 후보·사용량 조회에서 제외된다.
    호출자가 state_lock 보유. 활성 슬롯 OFF는 거부."""
    reconcile_active_slot(st)
    s = state.slot_of(st, n)
    if not s:
        raise LimitPilotError(_t("slot_not_registered", n=n))
    if enabled and s.get("kind") == "setup-token":
        raise LimitPilotError(
            _t("cannot_enable_setup_token", n=n)
        )
    if not enabled and st.get("activeSlot") == n:
        raise LimitPilotError(_t("cannot_disable_active"))
    s["enabled"] = enabled
    state.save_state(st)
    logutil.log(_t("slot_toggled", n=n, email=s.get("email"), state="ON" if enabled else "OFF"))
    return s.get("email")


def set_slot_alias(st: dict, n: int, alias) -> str:
    """슬롯 표시 별칭만 변경한다. credentials/활성 계정에는 손대지 않는다.

    호출자가 state_lock을 보유해야 한다. 공백 문자열은 별칭 지우기로 취급한다.
    """
    s = state.slot_of(st, n)
    if not s:
        raise LimitPilotError(_t("slot_not_registered", n=n))
    if alias is not None and not isinstance(alias, str):
        raise LimitPilotError(_t("alias_not_string"))
    value = (alias or "").strip() or None
    if value is not None and len(value) > 80:
        raise LimitPilotError(_t("alias_too_long"))
    s["alias"] = value
    state.save_state(st)
    return state.slot_label(s, n)


def live_slot_of(st: dict):
    """기본 Claude 프로필(~/.claude.json)의 현재 로그인이 가리키는 등록 슬롯."""
    try:
        live_oauth = filestore.read_json(paths.CLAUDE_JSON).get("oauthAccount") or {}
    except (OSError, json.JSONDecodeError):
        live_oauth = {}
    return (find_slot_by_uuid(st, live_oauth) if identity_uuid_pair(live_oauth)
            else find_slot_by_identity(st, live_oauth))


def register_current_login(st: dict) -> tuple:
    """Register the account Claude Code is signed in with → (slot, e-mail, is_new).

    Copies the active credential into a backup slot (read-back verified) and marks that
    slot active. Registering an account that already has a slot updates that slot and
    keeps the user's alias and per-account limits (``USER_SLOT_KEYS``). The caller holds
    the state lock. Raises LimitPilotError with a user-facing message when there is
    nothing usable to register.
    """
    blob = creds.kc_read_active()
    if not blob:
        raise LimitPilotError(i18n.t("add_no_login"))
    if blob.startswith("sk-ant-"):
        raise LimitPilotError(i18n.t("add_api_key"))
    from limitpilot import oauth as oauth_mod

    if not oauth_mod.parse_oauth(blob):
        raise LimitPilotError(i18n.t("add_bad_format"))
    try:
        oauth_account = filestore.read_json(paths.CLAUDE_JSON).get("oauthAccount")
    except (OSError, json.JSONDecodeError):
        oauth_account = None
    if not oauth_account or not oauth_account.get("emailAddress"):
        raise LimitPilotError(i18n.t("add_no_account"))
    session.validate_login_credentials(blob, oauth_account)
    email = oauth_account["emailAddress"]

    n = find_slot_by_identity(st, oauth_account)
    is_new = n is None
    if n is None:
        n = state.next_free_slot(st)  # 슬롯 수 제한 없음 — 항상 새 슬롯 할당

    creds.kc_write_slot(n, blob)
    prev = state.slot_of(st, n) or {}
    st["slots"][str(n)] = _keep_user_settings(prev, {
        "email": email,
        "orgName": oauth_account.get("organizationName") or prev.get("orgName"),
        "addedAt": prev.get("addedAt") or datetime.now(timezone.utc).isoformat(),
        "oauthAccount": state.extract_identity(oauth_account),
        "usage": prev.get("usage"),
        "usageFetchedAt": prev.get("usageFetchedAt"),
        "quarantine": None,  # 다시 등록하면 '다시 로그인 필요' 상태가 풀린다
        "enabled": True,     # 다시 등록은 명시적 사용 의사 — OFF 도 해제
        "kind": "oauth",
    })
    if st.get("activeSlot") != n:
        st["manualChoiceAt"] = time.time()  # 사용자가 직접 로그인한 계정 — 선제 이동 유예
    st["activeSlot"] = n
    state.save_state(st)
    return n, email, is_new
