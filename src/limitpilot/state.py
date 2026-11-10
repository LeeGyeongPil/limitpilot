"""State file (``~/.limitpilot/state.json``) and settings.

The state file holds slot metadata, usage snapshots and settings — never tokens.
Tokens live only in the OS credential store (``store/``).
"""

import json
import os

from limitpilot.i18n import t as _t
from limitpilot.constants import (DEFAULT_PRIORITY, LANGUAGES, PRIORITIES, STATE_SCHEMA,
                                  THRESHOLD_MAX, THRESHOLD_MIN, THRESHOLD_PCT)
from limitpilot.errors import LimitPilotError
from limitpilot import i18n
from limitpilot import paths
from limitpilot.store import filestore


# -------------------------------------------------------------------- state


def default_state() -> dict:
    return {
        "schemaVersion": STATE_SCHEMA,
        "activeSlot": None,
        "lastSwitchAt": None,
        "lastSwitchTo": None,
        "unhealthyTicks": 0,
        "lastNotify": {},
        "slots": {},
    }


def load_state() -> dict:
    try:
        st = filestore.read_json(paths.state_file())
        if st.get("schemaVersion") != STATE_SCHEMA:
            raise ValueError("schema mismatch")
        return st
    except (OSError, ValueError, json.JSONDecodeError):
        return default_state()


def save_state(st: dict) -> None:
    os.makedirs(paths.STATE_DIR, mode=0o700, exist_ok=True)
    filestore.atomic_write_json(paths.state_file(), st)


def get_settings(st: dict) -> dict:
    """조절 가능한 설정 — 미설정 시 기본값. 범위는 set_setting에서 강제."""
    s = st.get("settings") or {}
    thr = s.get("threshold", THRESHOLD_PCT)
    try:
        thr = min(THRESHOLD_MAX, max(THRESHOLD_MIN, float(thr)))
    except (TypeError, ValueError):
        thr = THRESHOLD_PCT
    lang = s.get("language")
    if lang not in LANGUAGES:
        lang = i18n.default_lang()
    # 공개 기본값(옵트인): 유지 호출·리셋 전 소진·자동 전환은 사용자가 켜야 동작한다.
    # 이전 도구에서 옮겨 온 사용자는 상태에 값이 저장돼 있어 그 값을 그대로 쓴다.
    keepalive = s.get("keepalive", False)
    if not isinstance(keepalive, bool):
        keepalive = False
    priority = s.get("priority")
    if priority not in PRIORITIES:
        priority = DEFAULT_PRIORITY
    # 주간 임계 미설정이면 세션 임계를 따른다(두 창 공통 임계)
    weekly = _clamp_threshold(s.get("weeklyThreshold"))
    final_burn = s.get("finalBurn", False)
    if not isinstance(final_burn, bool):
        final_burn = False
    auto_switch = s.get("autoSwitch", False)
    if not isinstance(auto_switch, bool):
        auto_switch = False
    return {"threshold": thr, "language": lang, "keepalive": keepalive,
            "priority": priority, "finalBurn": final_burn, "autoSwitch": auto_switch,
            "weeklyThreshold": thr if weekly is None else weekly,
            "weeklyThresholdSet": weekly is not None}


def _clamp_threshold(v):
    """저장된 임계값 해석 — 숫자가 아니면 None(미설정), 범위 밖은 범위로 자른다."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    return min(THRESHOLD_MAX, max(THRESHOLD_MIN, float(v)))


def _parse_threshold(value, what: str):
    """입력 임계값 검증 → float | None(None·''·'default'는 기본값으로 되돌림)."""
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "default", "-")):
        return None
    if isinstance(value, bool):
        raise LimitPilotError(_t("must_be_number", what=what, value=value))
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise LimitPilotError(_t("must_be_number", what=what, value=value)) from None
    if not (THRESHOLD_MIN <= v <= THRESHOLD_MAX):
        raise LimitPilotError(_t("must_be_in_range", what=what, lo=f"{THRESHOLD_MIN:.0f}",
                                 hi=f"{THRESHOLD_MAX:.0f}"))
    return round(v, 1)


def _parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    v = str(value).strip().lower()
    if v in ("on", "true", "1", "yes"):
        return True
    if v in ("off", "false", "0", "no"):
        return False
    raise LimitPilotError(_t("must_be_on_off"))


def set_setting(st: dict, key: str, value) -> dict:
    """설정 변경(범위 검증). 반환: 정규화된 전체 설정. 호출자가 state_lock 보유."""
    cur = get_settings(st)
    if key == "threshold":
        v = _parse_threshold(value, _t("lbl_threshold"))
        if v is None:  # 빈 값·default 는 기본 임계로 되돌린다
            v = THRESHOLD_PCT
        cur["threshold"] = v
    elif key == "language":
        v = str(value).strip().lower()
        if v not in LANGUAGES:
            raise LimitPilotError(_t("must_be_one_of", what=_t("lbl_language"),
                                 choices=" | ".join(sorted(LANGUAGES))))
        cur["language"] = v
    elif key == "keepalive":
        cur["keepalive"] = _parse_bool(value)
    elif key == "finalBurn":
        cur["finalBurn"] = _parse_bool(value)
    elif key == "autoSwitch":
        cur["autoSwitch"] = _parse_bool(value)
    elif key == "priority":
        v = str(value).strip().lower()
        if v not in PRIORITIES:
            raise LimitPilotError(_t("must_be_one_of", what=_t("lbl_priority"),
                                 choices=" | ".join(sorted(PRIORITIES))))
        cur["priority"] = v
    elif key == "weeklyThreshold":
        v = _parse_threshold(value, _t("lbl_weekly_threshold"))
        merged = dict(st.get("settings") or {})
        if v is None:
            merged.pop("weeklyThreshold", None)  # 세션 임계를 따름
        else:
            merged["weeklyThreshold"] = v
        st["settings"] = merged
        cur = get_settings(st)
    else:
        raise LimitPilotError(_t("unknown_setting", key=key))
    # 알 수 없는 키는 보존 — 위젯/트레이가 읽는 수동 설정(refreshSec 등)을 지우지 않는다.
    # 파생값(weeklyThreshold 실효값·weeklyThresholdSet)은 저장하지 않는다
    stored = {k: v for k, v in cur.items() if k not in ("weeklyThreshold", "weeklyThresholdSet")}
    st["settings"] = {**(st.get("settings") or {}), **stored}
    save_state(st)
    return cur


def slot_of(st: dict, n: int):
    return st["slots"].get(str(n))


def slot_label(s: dict, n: int) -> str:
    """표시명 우선순위: 별칭 > 이메일 > 'Slot N'."""
    s = s or {}
    return s.get("alias") or s.get("email") or i18n.t("slot_n", n=n)


def registered_slots(st: dict):
    """등록된 슬롯 번호 오름차순. 개수 제한 없음."""
    return sorted(int(k) for k, v in st.get("slots", {}).items() if v)


def next_free_slot(st: dict) -> int:
    used = set(registered_slots(st))
    n = 1
    while n in used:
        n += 1
    return n


def find_slot_by_email(st: dict, email):
    if not email:
        return None
    for n in registered_slots(st):
        if (slot_of(st, n) or {}).get("email") == email:
            return n
    return None


def extract_identity(oauth_account: dict) -> dict:
    # ~/.claude.json에 스플라이스할 계정 식별 객체 — 원본 필드 전체를 보존해 이식
    return dict(oauth_account)
