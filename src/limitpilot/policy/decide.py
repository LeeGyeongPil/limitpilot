"""Switch decision: (action, target, reason) for one tick — pure logic, no I/O.

action is ``switch`` / ``none`` / ``blocked``. Candidates are every other registered
slot that is enabled, not "needs login" and not manual-only (gateway, enterprise),
ordered by the ``priority`` setting: ``expiring`` (weekly reset soonest first) or
``balanced`` (lowest usage first).
"""

import time

from limitpilot.constants import (BURN_COOLDOWN_S, BURN_MANUAL_HOLD_S, BURN_RANK_BASE,
                                  COOLDOWN_S, EXPIRING_BUCKET_S, FINAL_MIN_ROOM_PCT,
                                  HYSTERESIS_PCT, MANUAL_HOLD_S, reason_all_exhausted,
                                  REBALANCE_COOLDOWN_S, REBALANCE_MIN_ROOM_PCT,
                                  REBALANCE_MIN_SESSION_ROOM_PCT, RESET_TIE_MAX_PCT,
                                  SESSION_WINDOW_S, TOKEN_EXPIRED, UNHEALTHY_TICKS, UNMANAGED,
                                  WEEKLY_CAP_MAX)
from limitpilot import i18n
from limitpilot.policy import limits
from limitpilot import state


def _r(ko: str, en: str) -> str:
    """Reason text in the current language (shown in logs and notifications)."""
    return en if i18n.current_lang() == "en" else ko


def candidate_score(pct, usage, now: float) -> float:
    """후보 정렬 점수(낮을수록 우선) = 구속 사용률 − 세션 리셋 보너스.

    보너스 = 리셋 임박도(지금이면 1, 5시간 뒤·미시작이면 0) ×
    min(RESET_TIE_MAX_PCT, 주간 창 여유). 임박도에 비례시켜 '리셋이 한참 남은
    소진 직전 계정'이 여유 있는 후보를 이기지 못하게 하고, 주간 여유로 캡해
    주간 창이 구속인 후보가 (주간을 풀어주지 못하는) 세션 리셋으로 이기지
    못하게 한다. 미시작 창은 보너스 0 — 리셋 정보가 없는 후보끼리는 종전과
    동일하게 사용률 순이 된다."""
    reset_in = max(0.0, limits.session_reset_epoch(usage, now) - now)
    soonness = 1.0 - min(reset_in, SESSION_WINDOW_S) / SESSION_WINDOW_S
    seven = usage.get("seven_day") if isinstance(usage, dict) else None
    weekly_left = max(0.0, 100.0 - float((seven or {}).get("pct") or 0.0))
    return pct - soonness * min(RESET_TIE_MAX_PCT, weekly_left)


def pick_candidate(cands, scores: dict, ranks: dict = None):
    """(사용률, 슬롯) 후보 중 candidate_score 최저를 선택 — 잔여량이 사실상
    같다면 소진 뒤 재충전(5시간 창 리셋)이 빨리 돌아오는 쪽이 낫다. 점수까지
    같으면 기존과 동일하게 사용률·슬롯 번호 순.

    ranks(expiring_rank)가 주어지면 그것이 1순위 — 주간 리셋이 임박해 쓰지 않으면
    사라질 여유분을 가진 후보부터 고른다(우선순위 expiring)."""
    inf = float("inf")
    return min(cands, key=lambda c: ((ranks or {}).get(c[1], inf), scores[c[1]], c))


def burn_rank(eff, now: float, base: float):
    """리셋 직전 소진 대상이면 순위(낮을수록 우선, 모든 리셋 임박 버킷보다 앞), 아니면 None.

    eff는 slot_decision_usage 결과. 주간 리셋 직전(final)이면 주간·세션 여유가 둘 다
    FINAL_MIN_ROOM_PCT 이상, 세션 리셋 직전(burn)이면 세션 여유가 그 이상이고 주간이 그
    계정의 임계 미만이어야 한다(세션을 태우면 주간도 쓰므로). 리셋이 이를수록 앞선다."""
    if not isinstance(eff, dict):
        return None
    fh, sd = eff.get("five_hour") or {}, eff.get("seven_day") or {}
    session_room = fh.get("room")
    if not fh.get("burn") or not isinstance(session_room, (int, float)) \
            or session_room < FINAL_MIN_ROOM_PCT:
        return None
    if sd.get("final"):
        if limits.weekly_headroom(eff) < FINAL_MIN_ROOM_PCT:
            return None
        t = limits.weekly_reset_epoch(eff, now)
    else:
        if isinstance(sd.get("pct"), (int, float)) and sd["pct"] >= base:
            return None
        t = limits.session_reset_at(eff, now)
    if t is None:
        return None
    return BURN_RANK_BASE + (t - now)


def expiring_rank(usage, now: float) -> float:
    """주간 리셋 임박 순위(낮을수록 우선) — 리셋 전에 쓰지 않으면 사라질 여유가
    EXPIRING_MIN_HEADROOM_PCT 이상 남은 계정만 순위를 받는다(12시간 단위 버킷).
    여유가 거의 없거나 창이 돌지 않는 계정은 inf — 종전 점수 순으로 밀린다.
    usage는 상한 환산값이어야 한다(slot_decision_usage)."""
    t = limits.weekly_reset_epoch(usage, now)
    if t is None or limits.weekly_headroom(usage) < limits.expiring_min_room(usage):
        return float("inf")
    return float(int((t - now) // EXPIRING_BUCKET_S))


def collect_candidates(st: dict, active, usages: dict, now: float,
                       snapshot_fallback: bool = False):
    """활성 외 자동전환 후보 수집 → (known, unknown, scores, ranks, eff).

    known=[(구속 사용률, 슬롯)] · unknown=[슬롯](사용률 불명) · scores=candidate_score ·
    ranks=expiring_rank(우선순위 expiring일 때만, 세션이 적격선 이하인 후보만) ·
    eff={슬롯: 상한 환산 usage}.
    격리·OFF·제한 토큰·수동 전용(엔터프라이즈·게이트웨이)은 제외. 사용률은 슬롯별
    주간 상한으로 환산한다 — 상한에 닿은 계정은 100%(소진)로 보여 후보에서 빠진다.
    snapshot_fallback이면 usages에 없는 슬롯은 저장된 스냅샷으로 대신한다."""
    settings = state.get_settings(st)
    expiring = settings["priority"] == "expiring"
    session_line = settings["threshold"] - HYSTERESIS_PCT
    known, unknown, scores, ranks, eff = [], [], {}, {}, {}
    for n in state.registered_slots(st):
        if n == active:
            continue
        s = state.slot_of(st, n)
        if (s.get("kind") == "setup-token" or s.get("quarantine")
                or not s.get("enabled", True) or limits.is_manual_only_slot(s)):
            continue
        u = (usages or {}).get(n)
        if not isinstance(u, dict):
            u = s.get("usage") if snapshot_fallback else None
        u = limits.slot_decision_usage(st, s, u) if isinstance(u, dict) else None
        p = limits.binding_pct(u)
        if p is None:
            unknown.append(n)
            continue
        known.append((p, n))
        scores[n] = candidate_score(p, u, now)
        eff[n] = u
        burn = burn_rank(u, now, settings["threshold"]) if settings["finalBurn"] else None
        if burn is not None:
            ranks[n] = burn  # 리셋 직전 소진 대상 — 우선순위 설정과 무관하게 최우선
        elif expiring:
            # 세션이 후보 적격선 위인 계정은 리셋 임박 우선권을 받지 않는다 — 곧
            # 세션 한도에 닿아 다시 전환될 계정을 주간 리셋만 보고 고르지 않게
            fh = (u.get("five_hour") or {}).get("pct") or 0.0
            ranks[n] = expiring_rank(u, now) if fh <= session_line else float("inf")
    return known, unknown, scores, ranks, eff


def best_candidate(st: dict, active, usages: dict = None):
    """활성 외 슬롯 중 최적 후보 = 격리 제외, 구속 사용률(lastGood/이번 틱) 최저.
    동급 사용률은 세션 리셋이 이른 슬롯 우선(candidate_score). 우선순위 expiring이면
    주간 리셋이 임박한 여유분 보유 슬롯이 먼저다. 사용률 불명 슬롯은 알려진 후보가
    없을 때의 차선. 후보 없으면 None."""
    known, unknown, scores, ranks, _ = collect_candidates(
        st, active, usages, time.time(), snapshot_fallback=True)
    if known:
        return pick_candidate(known, scores, ranks)[1]
    return unknown[0] if unknown else None


def decide(st: dict, usages: dict):
    """한 틱의 결정 → (action, target, reason). action: "switch"|"none"|"blocked".
    후보는 활성 외 전체 슬롯(N계정) — 격리 제외, 구속 사용률 최저 우선."""
    active = st.get("activeSlot")
    if active is None:
        return "none", None, _r("관리되지 않는 활성 계정", "active account is not managed")
    au = usages.get(active)
    now = time.time()

    # 수동 로그인(미등록 계정 활성) — 사용자의 명시적 선택을 데몬이 덮어쓰지 않는다
    if au == UNMANAGED:
        st["unhealthyTicks"] = 0
        return "none", None, _r("활성 계정이 관리 밖(수동 로그인) — add 등록 대기",
                                "signed in with an unregistered account — run `add` to register it")

    # 활성 토큰 만료 — 관망(fail-safe). 유휴 중엔 소모가 없고, Claude Code가
    # 다음 사용 시 스스로 갱신하므로 전환할 이유가 없다.
    if au == TOKEN_EXPIRED:
        st["unhealthyTicks"] = 0
        return "none", None, _r("활성 토큰 만료 — 관망(다음 사용 시 자동 갱신)",
                                "active token expired — waiting (Claude Code renews it on next use)")

    # 엔터프라이즈 활성 — 세션/주간 창이 없어(월 달러 한도) binding_pct가 항상
    # 불명이다. 이를 방치하면 '사용량 불명' 누적으로 구독 계정에 거짓 failover된다
    # (사용자가 수동으로 고른 엔터프라이즈를 데몬이 멋대로 갈아치우는 사고). 사용자가
    # 명시적으로 고른 계정이므로 자동 전환은 하지 않고 항상 관망한다.
    if limits.is_manual_only_slot(state.slot_of(st, active)):
        st["unhealthyTicks"] = 0
        if limits.is_gateway_slot(state.slot_of(st, active)):
            return "none", None, _r("서드파티 게이트웨이 활성 — 수동 전용, 자동 전환 보류",
                                    "third-party gateway active — manual only")
        return "none", None, _r("엔터프라이즈 활성 — 월 달러 한도(수동 전용), 자동 전환 보류",
                                "enterprise account active — monthly spend limit, manual only")

    settings = state.get_settings(st)
    threshold = settings["threshold"]
    active_slot = state.slot_of(st, active)
    au_eff = limits.slot_decision_usage(st, active_slot, au) if isinstance(au, dict) else au
    active_pct = limits.binding_pct(au_eff)
    if active_pct is None:
        st["unhealthyTicks"] = st.get("unhealthyTicks", 0) + 1
        if st["unhealthyTicks"] < UNHEALTHY_TICKS:
            return "none", None, _r(f"사용량 불명({st['unhealthyTicks']}/{UNHEALTHY_TICKS})",
                                    f"usage unknown ({st['unhealthyTicks']}/{UNHEALTHY_TICKS})")
        trigger = "failover"
    else:
        st["unhealthyTicks"] = 0
        if active_pct < threshold:
            if settings["finalBurn"]:
                moved = burn_rebalance(st, active, au_eff, usages, threshold, now)
                if moved:
                    return moved
            if settings["priority"] == "expiring":
                moved = expiring_rebalance(st, active, au_eff, usages, threshold, now)
                if moved:
                    return moved
            return "none", None, (_r("임계 미만(", "below threshold (")
                                  + limits.limits_summary(st, active_slot, au) + ")")
        trigger = "at-limit" if active_pct >= 100.0 else "proactive"

    # 후보 수집: (사용률, 슬롯) — 격리·엔터프라이즈 제외. known=이번 틱 사용률 확보,
    # unknown=불명. 엔터프라이즈는 정책상 자동 전환 대상이 아니다(수동 전용).
    known, unknown, scores, ranks, _ = collect_candidates(st, active, usages, now)
    if not known and not unknown:
        return "blocked", None, _r("전환 대상 없음(미등록/격리/OFF)",
                                   "no account to switch to (none registered, needs login or OFF)")

    if trigger == "proactive":
        last = st.get("lastSwitchAt")
        if last and now - last < COOLDOWN_S:
            return "none", None, _r("쿨다운", "cooldown")
        eligible = [(p, n) for p, n in known
                    if p <= threshold - HYSTERESIS_PCT and p < active_pct]
        if not eligible:
            if not known:
                return "blocked", None, _r("후보 사용량 불명 — 선제 전환 보류",
                                           "candidate usage unknown — not switching yet")
            return "blocked", None, _r(f"적격 후보 없음(최저 {min(known)[0]:.0f}%)",
                                       f"no eligible candidate (lowest {min(known)[0]:.0f}%)")
        best_pct, best = pick_candidate(eligible, scores, ranks)
        return "switch", best, _r(f"선제 전환(활성 {active_pct:.0f}% ≥ "
                                  f"{threshold:.0f}%, 후보 {best_pct:.0f}%)",
                                  f"threshold reached (active {active_pct:.0f}% >= "
                                  f"{threshold:.0f}%, candidate {best_pct:.0f}%)")

    # at-limit / failover: 쿨다운·히스테리시스 우회 — 잔여 있는 후보 중 최저 사용률
    active_lim = limits.slot_limits(st, active_slot, au, now)
    if trigger == "at-limit" and active_lim["cap"] < WEEKLY_CAP_MAX and limits.weekly_cap_reached(active_slot, au):
        limit_reason = _r(f"주간 상한 {active_lim['cap']:.0f}% 도달 — 즉시 전환",
                          f"weekly max {active_lim['cap']:.0f}% reached — switching now")
    else:
        limit_reason = _r("한도 도달 — 즉시 전환", "limit reached — switching now")
    alive = [(p, n) for p, n in known if p < 100.0]
    if alive:
        _, best = pick_candidate(alive, scores, ranks)
        st["unhealthyTicks"] = 0  # 전환으로 상황 종료 — 새 활성에 카운터 승계 금지
        return "switch", best, (limit_reason if trigger == "at-limit"
                                else _r("활성 계정 사용 불가 — 대안으로 전환",
                                        "active account unusable — switching to another"))
    if unknown:
        if trigger == "at-limit":
            # 데이터 없는 전환은 활성 소진이 '측정된' 경우에만 허용(최소 근거:
            # 비격리·등록). 활성이 측정 불가(failover)일 때 미지 후보로 옮기면
            # 네트워크/usage API 장애 동안 두 계정을 핑퐁하며 스래싱하게 된다
            st["unhealthyTicks"] = 0
            return "switch", unknown[0], limit_reason
        return "blocked", None, _r("활성 사용량 측정 불가 + 후보 사용량 불명 — 관망",
                                   "active and candidate usage both unknown — waiting")
    return "blocked", None, reason_all_exhausted()


def burn_rebalance(st: dict, active, au_eff, usages: dict, threshold: float, now: float):
    """리셋 직전 소진 대상 계정으로의 최우선 이동 → decide 반환형 | None.

    주간 리셋 5시간 전·세션 리셋 30분 전 계정의 남은 한도는 곧 사라지므로, 활성이 임계
    미만이어도 그 계정으로 옮겨 다 쓴다(리셋이 가장 이른 대상부터). 활성 자신이 소진
    대상이면 옮기지 않는다. 창이 짧아 쿨다운(BURN_COOLDOWN_S)·수동 선택 유예
    (BURN_MANUAL_HOLD_S)를 일반 선제 이동보다 짧게 둔다."""
    if burn_rank(au_eff, now, threshold) is not None:
        return None
    last = st.get("lastSwitchAt")
    if last and now - last < BURN_COOLDOWN_S:
        return None
    manual = st.get("manualChoiceAt")
    if isinstance(manual, (int, float)) and now - manual < BURN_MANUAL_HOLD_S:
        return None
    known, _, scores, ranks, eff = collect_candidates(st, active, usages, now)
    # 소진 대상만 — burn_rank는 BURN_RANK_BASE+초(최대 주간 창 길이) 범위
    burn_ceiling = BURN_RANK_BASE + 7 * 86400
    targets = [(p, n) for p, n in known if ranks.get(n, float("inf")) <= burn_ceiling]
    if not targets:
        return None
    _, best = pick_candidate(targets, scores, ranks)
    u = eff[best]
    if (u.get("seven_day") or {}).get("final"):
        mins = (limits.weekly_reset_epoch(u, now) - now) / 60
        what, room = _r("주간", "weekly"), limits.weekly_headroom(u)
    else:
        mins = (limits.session_reset_at(u, now) - now) / 60
        what, room = _r("세션", "session"), (u.get("five_hour") or {}).get("room") or 0.0
    when = (_r(f"{mins / 60:.1f}시간", f"{mins / 60:.1f} h") if mins >= 60
            else _r(f"{mins:.0f}분", f"{mins:.0f} min"))
    return "switch", best, _r(f"리셋 직전 소진(슬롯 {best} {what} {when} 뒤 리셋, 여유 {room:.0f}%)",
                              f"use up before reset (slot {best} {what} resets in {when}, "
                              f"{room:.0f}% left)")


def expiring_rebalance(st: dict, active, au_eff, usages: dict, threshold: float,
                       now: float):
    """임계 미만인 활성에서 '곧 사라질 주간 여유'를 가진 계정으로 선제 이동할지 판단
    → decide 반환형 튜플 | None (우선순위 expiring 전용).

    대상: 주간 리셋이 EXPIRING_SOON_S 안이고 상한 환산 여유가 EXPIRING_MIN_HEADROOM_PCT
    이상이며, 세션이 임계보다 REBALANCE_MIN_SESSION_ROOM_PCT 이상 낮고 주간(환산)이 임계보다
    REBALANCE_MIN_ROOM_PCT 이상 낮은 후보 — 주간 사용률이 높은 것이 곧 '사라질 여유가
    남은' 상태이므로 주간에는 일반 후보의 히스테리시스를 걸지 않는다. 활성 자신이
    이미 그런 계정이면 이동하지 않고, REBALANCE_COOLDOWN_S 안의 재전환도 막는다 —
    여유분 계정끼리 핑퐁하지 않게. 사용자가 직접 고른 계정(수동 전환·외부 /login)은
    MANUAL_HOLD_S 동안 그대로 둔다."""
    if limits.expiring_soon(au_eff, now) or burn_rank(au_eff, now, threshold) is not None:
        return None  # 활성 자신이 곧 사라질 여유를 쓰는 중(리셋 임박·리셋 직전 소진)
    last = st.get("lastSwitchAt")
    if last and now - last < REBALANCE_COOLDOWN_S:
        return None
    manual = st.get("manualChoiceAt")
    if isinstance(manual, (int, float)) and now - manual < MANUAL_HOLD_S:
        return None
    known, _, scores, ranks, eff = collect_candidates(st, active, usages, now)
    def movable(u):
        fh = (u.get("five_hour") or {}).get("pct") or 0.0
        sd = u.get("seven_day") or {}
        # 리셋 직전 소진 구간이면 주간 임계를 넘긴 계정도 최대치까지의 여유만 보면 된다
        weekly_ok = (limits.weekly_headroom(u) >= FINAL_MIN_ROOM_PCT if sd.get("final")
                     else (sd.get("pct") or 0.0) <= threshold - REBALANCE_MIN_ROOM_PCT)
        return (fh <= threshold - REBALANCE_MIN_SESSION_ROOM_PCT
                and weekly_ok and limits.expiring_soon(u, now))

    targets = [(p, n) for p, n in known if movable(eff[n])]
    if not targets:
        return None
    _, best = pick_candidate(targets, scores, ranks)
    hours = max(0.0, (limits.weekly_reset_epoch(eff[best], now) - now) / 3600.0)
    room = limits.weekly_headroom(eff[best])
    return "switch", best, _r(f"리셋 임박 우선(슬롯 {best} 주간 {hours:.0f}시간 뒤 리셋, "
                              f"여유 {room:.0f}%)",
                              f"expiring first (slot {best} weekly resets in {hours:.0f} h, "
                              f"{room:.0f}% left)")
