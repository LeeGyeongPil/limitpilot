"""Per-account limits: session / weekly thresholds, weekly max, enterprise monthly spend.

Pure functions over usage snapshots. Each window is normalised so that the account's own
threshold maps to the global session threshold and its maximum maps to 100 %; the
decision code then applies one set of rules to every account.
"""

import time
from datetime import datetime

from limitpilot.i18n import t as _t
from limitpilot.constants import (ENTERPRISE_ORG_TYPE, EXPIRING_MIN_HEADROOM_PCT, EXPIRING_SOON_S, FINAL_BURN_S, FINAL_MIN_ROOM_PCT, GATEWAY_KIND, SESSION_BURN_S, SESSION_WINDOW_S, STALE_OK_S, THRESHOLD_PCT, WEEKLY_CAP_MAX, WEEKLY_CAP_MIN)
from limitpilot.errors import (LimitPilotError)
from limitpilot import logutil
from limitpilot import state


def binding_pct(usage):
    """결정에 쓰는 구속 사용률 = 두 창 중 최대치. 불명이면 None.

    엔터프라이즈의 spend(달러 소진율)는 여기에 섞지 않는다 — 자동전환 결정은
    오직 구독 계정의 세션/주간 %로만 하고, 엔터프라이즈는 정책상 후보에서
    제외·활성 시 관망하기 때문(is_enterprise_slot·decide 참조)."""
    if not isinstance(usage, dict):
        return None
    pcts = [w["pct"] for w in (usage.get("five_hour"), usage.get("seven_day")) if w]
    return max(pcts) if pcts else None


def spend_pct(usage):
    """엔터프라이즈 달러 소진율(%). spend 없으면 None."""
    if not isinstance(usage, dict):
        return None
    spend = usage.get("spend")
    if isinstance(spend, dict) and isinstance(spend.get("pct"), (int, float)):
        return float(spend["pct"])
    return None


def gate_pct(usage):
    """실측 케이던스·probe_all 판정용 사용률 — 구독은 세션·주간 최대, 엔터프라이즈는
    달러 소진율. 전환 결정이 아니라 '얼마나 자주 재조회할지'에만 쓴다."""
    p = binding_pct(usage)
    return p if p is not None else spend_pct(usage)


def session_reset_epoch(usage, now: float) -> float:
    """후보의 5시간 창 리셋 예상 epoch. 창이 돌고 있으면 그 resets_at, 미시작·
    만료·불명이면 전환 후 첫 사용 시점에 새 창이 시작되므로 now+5h로 본다."""
    w = usage.get("five_hour") if isinstance(usage, dict) else None
    ra = (w or {}).get("resets_at")
    if ra:
        try:
            t = datetime.fromisoformat(ra.replace("Z", "+00:00")).timestamp()
            if t > now:
                return t
        except (ValueError, TypeError, AttributeError):
            pass
    return now + SESSION_WINDOW_S


def weekly_cap(s) -> float:
    """슬롯의 주간 사용 상한(%). 미설정·이상값은 100(상한 없음)."""
    v = (s or {}).get("weeklyCap", WEEKLY_CAP_MAX)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return WEEKLY_CAP_MAX
    if v != v:  # NaN
        return WEEKLY_CAP_MAX
    return min(WEEKLY_CAP_MAX, max(WEEKLY_CAP_MIN, v))


def slot_limits(st: dict, s, usage=None, now: float = None) -> dict:
    """슬롯의 실효 한도 → {"session", "weekly", "cap", "sessionOverride", "weeklyOverride",
    "weeklyBase", "capBase", "finalBurn"}. 계정별 임계가 없으면 전역값, 주간 임계는
    최대치를 넘지 않는다.

    usage(실측)가 주어지고 리셋 직전 소진이 켜져 있으면 — 남은 한도는 리셋되면 사라지므로:
    · 주간 리셋이 FINAL_BURN_S 안: 세션·주간 임계와 최대치를 모두 100(finalBurn=True)
    · 세션 리셋이 SESSION_BURN_S 안이고 주간이 임계·최대치보다 FINAL_MIN_ROOM_PCT 이상 아래:
      세션 임계만 100
      (sessionBurn=True, 주간 한도는 그대로 — 주간을 이미 넘겼으면 하지 않음)
    원래 값은 sessionBase·weeklyBase·capBase."""
    cfg = state.get_settings(st)
    t5 = state._clamp_threshold((s or {}).get("sessionThreshold"))
    t7 = state._clamp_threshold((s or {}).get("weeklyThreshold"))
    cap = weekly_cap(s)
    weekly = min(cap, cfg["weeklyThreshold"] if t7 is None else t7)
    session = cfg["threshold"] if t5 is None else t5
    final = session_burn = False
    if cfg["finalBurn"] and isinstance(usage, dict):
        now = time.time() if now is None else now
        reset = weekly_reset_epoch(usage, now)
        final = reset is not None and reset - now <= FINAL_BURN_S
        s_reset = session_reset_at(usage, now)
        sd = usage.get("seven_day") or {}
        weekly_pct = sd.get("pct") if isinstance(sd.get("pct"), (int, float)) else 0.0
        # 세션 소진은 주간 임계·최대치 아래일 때만 — 세션을 태우면 주간도 쓰기 때문
        session_burn = (s_reset is not None and s_reset - now <= SESSION_BURN_S
                        and weekly_pct <= weekly - FINAL_MIN_ROOM_PCT)
    return {"session": WEEKLY_CAP_MAX if (final or session_burn) else session,
            "sessionBase": session,
            "weekly": WEEKLY_CAP_MAX if final else weekly, "weeklyBase": weekly,
            "cap": WEEKLY_CAP_MAX if final else cap, "capBase": cap,
            "sessionOverride": t5 is not None, "weeklyOverride": t7 is not None,
            "weeklyOverrideValue": t7,
            "finalBurn": final, "sessionBurn": session_burn and not final}


def session_reset_at(usage, now: float):
    """세션(5시간) 창의 리셋 epoch — 창이 돌고 있지 않거나 불명이면 None."""
    w = usage.get("five_hour") if isinstance(usage, dict) else None
    ra = (w or {}).get("resets_at")
    if not ra:
        return None
    try:
        t = datetime.fromisoformat(ra.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None
    return t if t > now else None


def normalize_pct(p: float, threshold: float, limit: float, base: float) -> float:
    """창 사용률 p를 결정 척도로 정규화 — threshold→base(전역 세션 임계), limit→100,
    그 사이·바깥은 선형. 계정별 임계·최대치가 전역과 같으면 항등(p 그대로)."""
    t = min(threshold, limit)
    if p >= limit:
        return 100.0 + (p - limit)  # 최대치 도달 — 임계와 같아도 즉시 전환이 우선
    if p <= t and t == base:
        return p  # 항등 구간 — 부동소수 오차로 정확히 임계인 값이 임계 미만이 되지 않게
    if p <= t:
        return p * base / t if t > 0 else 100.0
    return base + (p - t) * (100.0 - base) / (limit - t)  # t < p < limit


def capped_usage(usage, cap: float, base: float = None, weekly_threshold: float = None):
    """주간 창만 정규화한 usage 사본(테스트·호환용) — slot_decision_usage 참조."""
    base = THRESHOLD_PCT if base is None else base
    wt = base if weekly_threshold is None else weekly_threshold
    if not isinstance(usage, dict):
        return usage
    sd = usage.get("seven_day")
    if not isinstance(sd, dict) or not isinstance(sd.get("pct"), (int, float)):
        return usage
    return {**usage, "seven_day": {**sd, "pct": normalize_pct(float(sd["pct"]), wt, cap, base)}}


def slot_decision_usage(st: dict, s, usage):
    """결정용 usage = 실측 usage의 세션·주간 창을 그 슬롯의 임계·최대치로 정규화한 사본.

    세션은 (세션 임계, 100), 주간은 (주간 임계, 주간 최대치)를 (전역 세션 임계, 100)에
    맞춘다. 결정 로직(임계·히스테리시스·즉시 전환·후보 점수·리셋 임박)은 이 값만 보면
    계정별 임계에서 선제 전환, 최대치에서 즉시 전환·후보 제외로 동작한다. 원본
    스냅샷(state)은 바꾸지 않는다 — 표시와 소진 판정(exhausted_until)은 실측치."""
    if not isinstance(usage, dict):
        return usage
    lim = slot_limits(st, s, usage)
    base = state.get_settings(st)["threshold"]
    out = dict(usage)
    for key, t, cap in (("five_hour", lim["session"], WEEKLY_CAP_MAX),
                        ("seven_day", lim["weekly"], lim["cap"])):
        w = usage.get(key)
        if isinstance(w, dict) and isinstance(w.get("pct"), (int, float)):
            entry = {**w, "pct": normalize_pct(float(w["pct"]), t, cap, base)}
            if key == "five_hour" and (lim["finalBurn"] or lim["sessionBurn"]):
                entry["burn"] = True
                entry["room"] = max(0.0, WEEKLY_CAP_MAX - float(w["pct"]))
            if key == "seven_day":
                # 주간 여유는 실측 기준(최대치까지 남은 %p) — 정규화 척도는 임계=최대치일 때
                # base→100으로 건너뛰어 최대치 직전 계정의 여유를 과대평가한다
                entry["room"] = max(0.0, cap - float(w["pct"]))
                if lim["finalBurn"]:
                    entry["final"] = True  # 리셋 직전 소진 구간 — 작은 여유도 쓸 가치가 있다
            out[key] = entry
    return out


def weekly_reset_epoch(usage, now: float):
    """주간 창의 리셋 epoch — 창이 돌고 있지 않거나(미시작·만료) 불명이면 None."""
    w = usage.get("seven_day") if isinstance(usage, dict) else None
    ra = (w or {}).get("resets_at")
    if not ra:
        return None
    try:
        t = datetime.fromisoformat(ra.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None
    return t if t > now else None


def weekly_headroom(usage) -> float:
    """주간 창 여유(%p). 결정용 usage(slot_decision_usage)면 실측 기준 '최대치까지
    남은 %p'(room), 아니면 100−사용률. 주간 정보가 없으면 100."""
    w = usage.get("seven_day") if isinstance(usage, dict) else None
    if isinstance(w, dict) and isinstance(w.get("room"), (int, float)):
        return max(0.0, float(w["room"]))
    try:
        return max(0.0, WEEKLY_CAP_MAX - float((w or {}).get("pct") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def expiring_min_room(usage) -> float:
    """리셋 임박 우선을 받을 최소 주간 여유 — 리셋 직전 소진 구간이면 FINAL_MIN_ROOM_PCT."""
    w = usage.get("seven_day") if isinstance(usage, dict) else None
    return FINAL_MIN_ROOM_PCT if isinstance(w, dict) and w.get("final") else EXPIRING_MIN_HEADROOM_PCT


def expiring_soon(usage, now: float) -> bool:
    """주간 여유가 EXPIRING_SOON_S 안에 리셋으로 사라질 계정인지."""
    t = weekly_reset_epoch(usage, now)
    return (t is not None and t - now <= EXPIRING_SOON_S
            and weekly_headroom(usage) >= expiring_min_room(usage))


def is_enterprise_slot(s) -> bool:
    """엔터프라이즈(달러 한도·수동 전용) 계정 슬롯인지 — 저장된 조직 유형으로 판정한다.
    네트워크/키체인 없이 state만으로 판단해 자동전환 정책(후보 제외·활성 시 관망)에 쓴다."""
    oa = (s or {}).get("oauthAccount") or {}
    return oa.get("organizationType") == ENTERPRISE_ORG_TYPE


def is_gateway_slot(s) -> bool:
    """Anthropic-format third-party gateway profile (manual selection only)."""
    return (s or {}).get("kind") == GATEWAY_KIND


def is_manual_only_slot(s) -> bool:
    """Slots that are displayed and manually selectable but never auto-selected."""
    return is_enterprise_slot(s) or is_gateway_slot(s)


def earliest_reset(usages) -> float:
    """알려진 resets_at 중 가장 이른 epoch. 없으면 0."""
    times = []
    for u in usages:
        if not isinstance(u, dict):
            continue
        for w in (u.get("five_hour"), u.get("seven_day")):
            if w and w.get("resets_at"):
                try:
                    times.append(datetime.fromisoformat(
                        w["resets_at"].replace("Z", "+00:00")).timestamp())
                except ValueError:
                    pass
    return min(times) if times else 0.0


def stale_or_none(s: dict):
    """조회 실패 시 대체값 — lastGood이 신선하고(≤STALE_OK_S) 사용 중인 창이
    아직 리셋되지 않았을 때만 결정에 사용(리셋 직후 낡은 100% 스냅샷으로 오전환
    방지). 0% 창은 리셋을 거쳐도 0%라 과대평가가 불가능하므로 항상 신뢰한다 —
    버리면 여유 100% 후보가 '사용량 불명'으로 강등돼 선제 전환·failover에서
    빠진다."""
    u, fetched = s.get("usage"), s.get("usageFetchedAt")
    now = time.time()
    if not u or not fetched or now - fetched > STALE_OK_S:
        return None
    for w in (u.get("five_hour"), u.get("seven_day")):
        if not w or w.get("pct", 0) <= 0:
            continue
        ra = w.get("resets_at")
        if not ra:
            return None
        try:
            if datetime.fromisoformat(ra.replace("Z", "+00:00")).timestamp() <= now:
                return None
        except (ValueError, TypeError, AttributeError):
            return None
    return u


def exhausted_until(s: dict):
    """lastGood이 한도 소진(≥100%)이고 그 창의 리셋이 미래면 리셋 epoch 반환.
    소진 계정은 API 요청 자체가 429로 거부되므로 리셋 전 재조회는 무의미하다."""
    u = (s or {}).get("usage") or {}
    for w in (u.get("five_hour"), u.get("seven_day")):
        if w and w.get("pct", 0) >= 100.0 and w.get("resets_at"):
            try:
                t = datetime.fromisoformat(
                    w["resets_at"].replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if t > time.time():
                return t
    return None


def set_slot_weekly_cap(st: dict, n: int, cap) -> float:
    """슬롯의 주간 사용 상한(%) 지정 — 100이면 상한 해제. 구독 OAuth 계정만.
    호출자가 state_lock 보유. 반환: 정규화된 상한."""
    s = state.slot_of(st, n)
    if not s:
        raise LimitPilotError(_t("slot_not_registered", n=n))
    if is_manual_only_slot(s) or s.get("kind") == "setup-token":
        raise LimitPilotError(_t("cap_subscription_only"))
    if isinstance(cap, bool):
        raise LimitPilotError(_t("must_be_number", what=_t("lbl_weekly_cap"), value=cap))
    try:
        v = float(cap)
    except (TypeError, ValueError):
        raise LimitPilotError(_t("must_be_number", what=_t("lbl_weekly_cap"), value=cap)) from None
    if not (WEEKLY_CAP_MIN <= v <= WEEKLY_CAP_MAX):  # NaN도 여기서 거부
        raise LimitPilotError(
            _t("weekly_cap_range", lo=f"{WEEKLY_CAP_MIN:.0f}", hi=f"{WEEKLY_CAP_MAX:.0f}"))
    v = float(round(v))
    if v >= WEEKLY_CAP_MAX:
        s.pop("weeklyCap", None)
    else:
        s["weeklyCap"] = v
    state.save_state(st)
    logutil.log(_t("slot_cap_set", n=n, email=s.get("email"), v=f"{v:.0f}"))
    return v


def capped_until(s, now: float, st: dict = None):
    """주간 상한에 닿은 스냅샷이고 그 주간 창 리셋이 미래면 리셋 epoch, 아니면 None.
    st가 주어지고 리셋 직전 소진 구간이면 None — 그 계정을 다시 쓸 수 있으니 실측한다."""
    u = (s or {}).get("usage")
    if not weekly_cap_reached(s, u):
        return None
    if st is not None and slot_limits(st, s, u, now)["finalBurn"]:
        return None
    return weekly_reset_epoch(u, now)


def limits_summary(st: dict, s, usage) -> str:
    """'세션 67%/95% · 주간 18%/90%' — 실측 사용률/실효 임계(로그·사유 표시용)."""
    from limitpilot import i18n

    en = i18n.current_lang() == "en"
    lim = slot_limits(st, s, usage)
    parts = []
    for key, name, t in (("five_hour", _t("limit_session"), lim["session"]),
                         ("seven_day", _t("limit_weekly"), lim["weekly"])):
        w = usage.get(key) if isinstance(usage, dict) else None
        if isinstance(w, dict) and isinstance(w.get("pct"), (int, float)):
            parts.append(f"{name} {w['pct']:.0f}%/{t:.0f}%")
    return " · ".join(parts) or _t("limit_partial_usage")


def set_slot_limits(st: dict, n: int, session=..., weekly=..., cap=...) -> dict:
    """계정별 세션 임계·주간 임계·주간 최대치 지정(...은 변경 안 함, None은 기본값).
    구독 OAuth 계정만. 호출자가 state_lock 보유. 반환: slot_limits."""
    s = state.slot_of(st, n)
    if not s:
        raise LimitPilotError(_t("slot_not_registered", n=n))
    if is_manual_only_slot(s) or s.get("kind") == "setup-token":
        raise LimitPilotError(_t("limits_subscription_only"))
    updates = {}
    if session is not ...:
        updates["sessionThreshold"] = state._parse_threshold(session, _t("lbl_session_threshold"))
    if weekly is not ...:
        updates["weeklyThreshold"] = state._parse_threshold(weekly, _t("lbl_weekly_threshold"))
    if cap is not ...:
        if cap is None or (isinstance(cap, str) and cap.strip().lower() in ("", "default", "-")):
            cap = WEEKLY_CAP_MAX
        try:
            unchanged = float(cap) == weekly_cap(s)
        except (TypeError, ValueError):
            unchanged = False
        if not unchanged:
            set_slot_weekly_cap(st, n, cap)  # 검증·저장·로그
    for key, v in updates.items():
        if v is None:
            s.pop(key, None)
        else:
            s[key] = v
    if updates:
        state.save_state(st)
        lim = slot_limits(st, s)
        logutil.log(_t("slot_limits_set", n=n, email=s.get("email"), s=f"{lim['session']:.0f}",
                       w=f"{lim['weekly']:.0f}", c=f"{lim['cap']:.0f}"))
    return slot_limits(st, s)


def weekly_cap_reached(s, usage) -> bool:
    """실측 주간 사용률이 슬롯의 주간 상한(100 미만으로 지정된 경우)에 닿았는지."""
    cap = weekly_cap(s)
    sd = usage.get("seven_day") if isinstance(usage, dict) else None
    return (cap < WEEKLY_CAP_MAX and isinstance(sd, dict)
            and isinstance(sd.get("pct"), (int, float)) and sd["pct"] >= cap)
