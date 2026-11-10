"""Usage collection: which slots to query this tick, token refresh, 429 back-off.

``collect_usage`` works on a *copy* of the state and may do network I/O; callers must not
hold the state lock while it runs. ``merge_changes`` then copies what it learned into
the freshly re-read state under the lock (only fields this run actually changed).
"""

import copy
import time
from datetime import datetime, timezone

from limitpilot.constants import (ACTIVE_PROBE_S, CANDIDATE_PROBE_S, FETCH_BACKOFF_BASE_S,
                                  FETCH_BACKOFF_CAP_S, FRESHEN_BUFFER_MS, MANUAL_REFRESH_MIN_S,
                                  PROBE_MARGIN_PCT, TOKEN_EXPIRED, UNMANAGED)
from limitpilot.errors import LimitPilotError, KeychainError
from limitpilot import accounts
from limitpilot import i18n
from limitpilot import locks
from limitpilot import logutil
from limitpilot import notify
from limitpilot import oauth as oauth_mod
from limitpilot.policy import limits
from limitpilot import state
from limitpilot.store import creds
from limitpilot import usage_api

# 수집 단계가 바꿀 수 있는 슬롯 필드 — 병합 때 이 필드만 옮긴다
_SLOT_FIELDS = ("usage", "usageFetchedAt", "fetchBackoff", "quarantine", "keepalive", "tokenExpiresAt",
                "manualRefreshAt")


def _same_account(a: dict, b: dict) -> bool:
    ida = (a or {}).get("oauthAccount") or {}
    idb = (b or {}).get("oauthAccount") or {}
    return ((a or {}).get("email") == (b or {}).get("email")
            and ida.get("accountUuid") == idb.get("accountUuid"))


def merge_changes(st: dict, base: dict, work: dict) -> None:
    """Copy into ``st`` every slot field / notification stamp that changed base → work.

    Slots that were removed or re-registered for another account meanwhile are skipped,
    and fields nobody touched in ``work`` never overwrite newer values in ``st``.
    """
    if st is work:
        return
    for key, new in (work.get("slots") or {}).items():
        old = (base.get("slots") or {}).get(key) or {}
        cur = (st.get("slots") or {}).get(key)
        if not cur or not new or not _same_account(cur, new):
            continue
        for field in _SLOT_FIELDS:
            if new.get(field) != old.get(field):
                cur[field] = copy.deepcopy(new.get(field))
    old_notes = base.get("lastNotify") or {}
    for kind, at in (work.get("lastNotify") or {}).items():
        if old_notes.get(kind) != at:
            st.setdefault("lastNotify", {})[kind] = at


def collect_and_merge(allow_refresh: bool = True):
    """Snapshot (lock) → collect (no lock) → merge + save (lock). Returns (state, usages)."""
    with locks.state_lock():
        st = state.load_state()
        accounts.reconcile_active_slot(st)
        base = copy.deepcopy(st)
    work = copy.deepcopy(base)
    usages = collect_usage(work, allow_refresh=allow_refresh)
    with locks.state_lock():
        st = state.load_state()
        accounts.reconcile_active_slot(st)
        merge_changes(st, base, work)
        state.save_state(st)
    return st, usages


def maybe_release_quarantine(st: dict, n: int) -> None:
    """격리 슬롯의 자격증명 지문이 격리 당시와 달라졌으면(외부 갱신) 격리 해제."""
    s = state.slot_of(st, n)
    q = (s or {}).get("quarantine")
    if not s or not q:
        return
    try:
        blob = creds.kc_read_slot(n)
    except KeychainError:
        return
    fp_now = oauth_mod.refresh_fingerprint(blob) if blob else None
    fp_then = q.get("refreshTokenFingerprint")
    if fp_now and fp_then is None:
        # 격리 시점 키체인 읽기 실패로 지문이 비어 있던 경우 — 해제하면 죽은
        # 토큰으로 무의미한 리프레시→재격리 플리커가 생기므로, 지문만 보충해
        # 이후 '자격증명 갱신' 비교가 가능하게 한다
        q["refreshTokenFingerprint"] = fp_now
        return
    if fp_now and fp_now != fp_then:
        s["quarantine"] = None
        logutil.log(i18n.t("log_unquarantine", n=n, email=s.get("email")))


def slot_access_oauth(st: dict, n: int, allow_refresh: bool = True):
    """슬롯 n의 사용 가능한 oauth(dict) 확보 → (oauth | None, err | None).

    활성 슬롯은 라이브 자격증명을 읽기만 한다 — 만료여도 절대 대신 리프레시하지
    않는다(Claude Code 소유, 회전 경합 방지). 비활성 슬롯은 만료 임박 시 리프레시해
    회전 토큰을 키체인에 되쓰고, 영구 실패면 격리한다. err:
    "unmanaged"(활성 키체인이 다른 계정) · "token-expired" · "no-credential" ·
    "keychain" · "keychain-write"(회전 토큰 반영 실패) · "quarantined" · "refresh-transient".
    상태 락은 필요 없다 — 토큰 갱신은 슬롯 락(locks.slot_lock)으로 직렬화한다.
    읽은 토큰의 만료 시각(비밀 아님)은 slot['tokenExpiresAt'] 에 남겨 대시보드가
    저장소를 다시 읽지 않고 '토큰 만료' 를 표시하게 한다."""
    s = state.slot_of(st, n) or {}
    if n == st.get("activeSlot"):
        # 활성 키체인의 주인이 이 슬롯인지 먼저 확인 — 사용자가 다른(미등록)
        # 계정으로 방금 /login했다면 그 토큰을 이 슬롯 몫으로 쓰면 안 된다
        if accounts.live_slot_of(st) != n:
            return None, "unmanaged"
        try:
            blob = creds.kc_read_active()
        except KeychainError:
            return None, "keychain"
        oauth = oauth_mod.parse_oauth(blob) if blob else None
        if not oauth:
            return None, "no-credential"
        _note_expiry(s, oauth)
        if oauth_mod.is_expired(oauth):
            return None, "token-expired"
        return oauth, None
    if s.get("quarantine"):
        return None, "quarantined"
    try:
        blob = creds.kc_read_slot(n)
    except KeychainError:
        return None, "keychain"
    oauth = oauth_mod.parse_oauth(blob) if blob else None
    if not oauth:
        return None, "no-credential"
    _note_expiry(s, oauth)
    if oauth_mod.is_expired(oauth, FRESHEN_BUFFER_MS):
        if not allow_refresh:
            return None, "token-expired"
        with locks.slot_lock(n):
            fresh, err = _refresh_slot_locked(st, n, blob)
        if fresh:
            _note_expiry(s, fresh)
        return fresh, err
    return oauth, None


def _note_expiry(slot: dict, oauth: dict) -> None:
    expires = oauth.get("expiresAt") if isinstance(oauth, dict) else None
    if isinstance(expires, (int, float)) and not isinstance(expires, bool):
        slot["tokenExpiresAt"] = expires


def _refresh_slot_locked(st: dict, n: int, blob: str):
    """Refresh slot ``n``'s token while holding its slot lock (state lock not needed)."""
    # 다른 프로세스가 방금 갱신했을 수 있다 — 락을 잡은 뒤 다시 읽어 확인한다
    try:
        latest = creds.kc_read_slot(n)
    except KeychainError:
        return None, "keychain"
    if latest and latest != blob:
        fresh = oauth_mod.parse_oauth(latest)
        if fresh and not oauth_mod.is_expired(fresh, FRESHEN_BUFFER_MS):
            return fresh, None
        blob = latest
    new_blob, outcome = oauth_mod.try_refresh(blob)
    if new_blob:
        # 서버가 refresh token 을 회전시켰으면 new_blob 이 유일한 사본 — 쓰기 실패로
        # 틱을 죽이면 토큰이 유실되고 다음 틱에 옛 토큰이 invalid_grant 를 받아 멀쩡한
        # 계정이 다시 로그인 필요 상태로 잘못 빠진다
        try:
            creds.kc_write_slot_retry(n, new_blob)
        except KeychainError as e:
            logutil.error(i18n.t("log_fatal_rotate", n=n, err=e))
            # 저장소에는 죽은(회전 전) 토큰만 남아 있다 — 후보로 광고하지 않는다
            return None, "keychain-write"
        return oauth_mod.parse_oauth(new_blob), None
    if outcome in ("invalid_grant", "no_refresh_token"):
        quarantine(st, n, outcome)
        return None, "quarantined"
    return None, "refresh-transient"  # 일시 오류 — 다음 기회에 재시도, 격리 금지


def near_switch(st: dict, s, usage, threshold: float):
    """활성 계정이 전환 판단 근처인지(실측을 매 틱으로 올릴지) → True | False | None(불명).

    평소엔 결정 척도(정규화)로 '임계−PROBE_MARGIN_PCT' 이상이면 근접. 리셋 전 소진
    구간에서는 주간이 정규화 척도에서 오래 임계 근처에 머물러(임계 대신 100까지 쓰므로)
    최대 5시간 동안 후보까지 매 분 조회하게 된다 — 그 구간은 세션이 임계 근처이거나
    주간이 늘어난 한도(100)에서 PROBE_MARGIN_PCT 안일 때만 근접으로 본다(429 예산)."""
    eff = limits.slot_decision_usage(st, s, usage)
    fh = (eff or {}).get("five_hour") if isinstance(eff, dict) else None
    sd = (eff or {}).get("seven_day") if isinstance(eff, dict) else None
    if (isinstance(sd, dict) and sd.get("final")) or (isinstance(fh, dict) and fh.get("burn")):
        # 소진 중인 창은 '늘어난 한도(100)까지의 실측 여유'로, 아닌 창은 평소 척도로 본다
        def near(w, final):
            if not isinstance(w, dict) or not isinstance(w.get("pct"), (int, float)):
                return False
            if final:
                return w.get("room", 0.0) <= PROBE_MARGIN_PCT
            return w["pct"] >= threshold - PROBE_MARGIN_PCT
        return near(fh, bool((fh or {}).get("burn"))) or near(sd, bool((sd or {}).get("final")))
    p = limits.gate_pct(eff)
    if p is None:
        return None
    return p >= threshold - PROBE_MARGIN_PCT


def record_usage_fetch(st: dict, n: int, usage, err, retry_after, now: float):
    """usage 조회 결과를 슬롯에 반영 — 성공은 스냅샷 갱신·백오프 해제, 429는
    지수 백오프(Retry-After 우선). 반환: 성공 usage 또는 None. 저장은 호출자 몫."""
    s = state.slot_of(st, n)
    bo = s.get("fetchBackoff") or {}
    if usage:
        s["usage"] = usage
        s["usageFetchedAt"] = time.time()
        if s.get("fetchBackoff"):
            s["fetchBackoff"] = None  # 회복 — 백오프 리셋
        return usage
    if err == "http-429":
        fails = (bo.get("fails") or 0) + 1
        delay = retry_after or min(
            FETCH_BACKOFF_BASE_S * 2 ** (fails - 1), FETCH_BACKOFF_CAP_S)
        delay = min(max(delay, 60.0), 3600.0)
        if n == st.get("activeSlot"):
            # 활성 슬롯 백오프는 STALE_OK_S(300s) 안에 재시도가 최소 2회
            # 보장되도록 상한 고정 — 더 길면 스냅샷이 stale로 굳어
            # decide가 멀쩡한 활성 계정을 거짓 failover한다
            delay = min(delay, FETCH_BACKOFF_BASE_S)
        s["fetchBackoff"] = {"fails": fails, "until": now + delay}
        logutil.log(i18n.t("log_fetch_429", n=n, mins=f"{delay / 60:.0f}"))
    else:
        logutil.log(i18n.t("log_fetch_fail", n=n, err=err))
    return None


def collect_usage(st: dict, allow_refresh: bool = True):
    """등록 슬롯들의 usage 수집 → {slot: normalized | TOKEN_EXPIRED | None}.

    usage API 호출 예산: 활성 슬롯만 매 틱 실측하고, 나머지(OFF 포함 —
    대시보드/트레이 표시용)는 CANDIDATE_PROBE_S 주기로만 실측한다. 단 활성이
    임계 근접/측정 불가면 후보(ON)를 매 틱으로 승격한다. 429를 맞은 슬롯은
    fetchBackoff(지수, Retry-After 우선)로 눌러 페널티 윈도우를 재점화하지 않는다.

    성공분은 st의 lastGood에 병합(저장은 호출자 몫). 호출자는 state_lock을 보유해야
    한다 — 동시 리프레시(refresh token 회전 경합)를 직렬화하기 위해 네트워크 왕복
    동안 락을 점유하는 코스-락 설계이며, 대기자는 타임아웃(30s) 내에 순서를 얻는다.
    """
    result = {}
    active = st.get("activeSlot")
    now = time.time()
    threshold = state.get_settings(st)["threshold"]

    def probe_all():
        # 활성이 임계에 근접했거나 실측 불가면 후보 데이터도 신선해야 전환
        # 결정이 정확하다 (활성 슬롯을 먼저 처리하므로 result에 이미 있음).
        # 단 decide가 어차피 관망하는 상태(미등록 활성/수동 로그인/토큰 만료/
        # 엔터프라이즈 활성)는 전환이 일어나지 않으므로 후보를 서둘러 실측할 이유가
        # 없다 — 429 예산 절약
        if active is None:
            return False
        if limits.is_manual_only_slot(state.slot_of(st, active)):
            return False
        au = result.get(active)
        if au in (UNMANAGED, TOKEN_EXPIRED):
            return False
        return near_switch(st, state.slot_of(st, active), au, threshold) is not False

    # 활성 슬롯 먼저 — 나머지 슬롯의 조회 승격 여부가 활성 실측값에 달려 있다
    for n in sorted(state.registered_slots(st), key=lambda k: k != active):
        s = state.slot_of(st, n)
        if limits.is_gateway_slot(s):
            result[n] = None
            continue
        if s.get("kind") == "setup-token":
            # setup-token은 user:inference 전용이라 usage/profile 권한이 없다.
            # 네트워크 요청과 자동전환을 모두 막고 UI에서 재등록을 안내한다.
            result[n] = None
            continue
        maybe_release_quarantine(st, n)
        # 격리 제외는 비활성 슬롯에만 — 활성 슬롯은 force 전환 등으로 격리
        # 상태일 수 있고, 여기서 None을 주면 decide가 멀쩡한 활성 계정을
        # '사용 불가'로 오판해 거짓 failover를 일으킨다(실측 사용량으로 판단해야 함)
        if n != active and s.get("quarantine"):
            result[n] = None
            continue
        if limits.exhausted_until(s) or limits.capped_until(s, now, st):
            # 소진(또는 주간 상한 도달) 확정 — 리셋 전까지 조회 생략(429 스팸 방지),
            # 스냅샷이 곧 진실. 상한을 올리면 capped_until이 풀려 즉시 재실측된다
            result[n] = s.get("usage")
            continue
        bo = s.get("fetchBackoff") or {}
        if now < (bo.get("until") or 0):
            # 429 백오프 중 — 재타격하면 페널티 윈도우가 연장/재점화된다
            result[n] = limits.stale_or_none(s)
            continue
        if n != active:
            # OFF 슬롯도 표시용으로 실측하되(후보 자격은 decide가 거른다) 승격
            # 없이 항상 CANDIDATE_PROBE_S 주기 — 전환 결정에 쓰이지 않기 때문.
            # 게이트 기준은 실측 신선도(429 예산) — 결정용 값은 stale_or_none이
            # 별도로 거른다(리셋 지난 스냅샷은 unknown 처리)
            fetched = s.get("usageFetchedAt")
            fresh = fetched and now - fetched < CANDIDATE_PROBE_S
            if fresh and (not s.get("enabled", True) or not probe_all()):
                result[n] = limits.stale_or_none(s)
                continue

        oauth, err = slot_access_oauth(st, n, allow_refresh=allow_refresh)
        if n == active:
            if err == "unmanaged":
                # 수동 로그인(미등록 계정) — 그 사용량을 이 슬롯에 귀속하면 안 되고,
                # failover로 수동 로그인을 덮어써서도 안 된다(관망)
                result[n] = UNMANAGED
                continue
            if err == "token-expired":
                # 활성 토큰은 Claude Code 소유 — 어떤 경우에도 대신 리프레시하지
                # 않는다(회전 경합·쓰기 실패 시 계정 로그인 상실 위험). 유휴 중엔
                # 소모도 없고, 다음 사용 시 Claude Code가 스스로 갱신한다.
                result[n] = TOKEN_EXPIRED
                continue
            if err:
                result[n] = None
                continue
            # 활성도 매 틱 실측하지 않는다 — 예산은 클라이언트(상태줄 등)와 공유되고
            # 실측된 지속 한도가 그보다 낮다. 임계 근접/불명 시에만 매 틱 승격.
            # 게이트 기준은 실측 신선도 — 3분 내 실측이고 임계에서 멀면 그 값을
            # 그대로 쓴다(resets_at 없는 0% 창도 스킵돼야 매 틱 재실측을 안 한다)
            fetched = s.get("usageFetchedAt")
            if fetched and now - fetched < ACTIVE_PROBE_S:
                # 엔터프라이즈는 세션 % 대신 달러 소진율로 케이던스 판단(급증 없음).
                # gate_pct가 spend 비율을 돌려주므로 신선+여유면 정기 주기까지 스킵된다.
                # 주간 상한은 환산값으로 본다 — 상한 근접도 '임계 근접'이다
                if near_switch(st, s, s.get("usage"), threshold) is False:
                    result[n] = s.get("usage")
                    continue
        elif err in ("token-expired", "refresh-transient"):
            # 리프레시 금지(dry-run) 또는 일시 오류 — 다음 틱 재시도, 격리 금지
            result[n] = limits.stale_or_none(s)
            continue
        elif err:
            # 자격증명을 읽지 못하거나(키체인) 회전 토큰 반영 실패·격리 — 전환해도
            # 실패하므로 스냅샷이 멀쩡해도 후보로 광고하지 않는다(사용량 불명)
            result[n] = None
            continue

        usage, err, retry_after = usage_api.fetch_usage(oauth["accessToken"])
        if record_usage_fetch(st, n, usage, err, retry_after, now):
            result[n] = usage
        else:
            result[n] = limits.stale_or_none(s)
    return result


def due_keepalive_slots(st: dict, now: float):
    """유지 호출이 필요한 슬롯 — 가장 오래 쉰 슬롯부터."""
    due = [n for n in state.registered_slots(st)
           if usage_api.keepalive_eligible(state.slot_of(st, n)) and usage_api.keepalive_due(state.slot_of(st, n), now)]
    return sorted(due, key=lambda n: ((state.slot_of(st, n).get("keepalive") or {}).get("at") or 0, n))


def keepalive_slot(st: dict, n: int, now: float = None) -> dict:
    """슬롯 n 유지 호출 1회 → 기록(dict). 결과는 slot['keepalive']에 남는다.

    비활성 계정은 만료 임박 토큰을 리프레시(회전 토큰 되쓰기·영구 실패 격리 포함)한
    뒤, 활성 계정은 유효한 라이브 토큰으로만(대신 리프레시 금지) 최소 메시지를
    보낸다. 한도 소진 계정은 메시지가 429로 거부되므로 토큰 갱신만 한다.
    호출자가 state_lock을 보유해야 하며 저장은 호출자 몫."""
    now = time.time() if now is None else now
    s = state.slot_of(st, n)
    if n != st.get("activeSlot"):
        maybe_release_quarantine(st, n)
    oauth, err = slot_access_oauth(st, n, allow_refresh=True)
    rec = {"at": now, "ok": False, "err": None, "retry": False}
    if not oauth:
        rec["err"] = err
        # 활성 토큰 만료(다음 사용 시 Claude Code가 갱신)·일시 오류는 곧 풀린다
        rec["retry"] = err in ("token-expired", "refresh-transient", "keychain", "unmanaged")
    elif limits.exhausted_until(s):
        rec.update(ok=True, note="refresh-only")
    else:
        ok, err, detail = usage_api.send_keepalive(oauth["accessToken"])
        transient = err in ("network", "error", "http-429") or (
            isinstance(err, str) and err.startswith("http-5"))
        rec.update(ok=ok, err=err, retry=bool(not ok and transient))
        if detail:
            rec["detail"] = detail
    s["keepalive"] = rec
    if rec["ok"]:
        logutil.log(i18n.t("log_keepalive_ok", n=n)
                    + (i18n.t("log_keepalive_exhausted") if rec.get("note") else ""))
    else:
        logutil.log(i18n.t("log_keepalive_fail", n=n, err=rec["err"])
            + (f" — {rec['detail']}" if rec.get("detail") else ""))
    return rec


def refresh_slot_usage(st: dict, n: int, now: float = None):
    """수동 사용량 새로고침(슬롯 1개) → (status, detail).

    status: "ok" · "recent"(MANUAL_REFRESH_MIN_S 안에 이미 실측, detail=실측 epoch) ·
    "recent-failed"(그 안의 직전 수동 시도가 실패 — 재시도 생략) ·
    "backoff"(429 백오프 중, detail=해제 epoch) · "skip"(게이트웨이·제한 토큰) ·
    "error"(detail=slot_access_oauth/fetch_usage 오류 종류).
    로그인되지 않은(비활성·OFF) 계정도 만료 토큰을 리프레시해 실측한다 — 활성은
    라이브 토큰이 유효할 때만(대신 리프레시 금지). 소진 계정도 사용자가 요청하면 조회한다.
    호출자가 state_lock 보유(+reconcile_active_slot), 저장은 호출자 몫."""
    now = time.time() if now is None else now
    s = state.slot_of(st, n)
    if not s:
        raise LimitPilotError(i18n.t("slot_not_registered", n=n))
    if limits.is_gateway_slot(s) or s.get("kind") == "setup-token":
        return "skip", None
    # 연타 방지는 성공 실측과 직전 수동 시도(실패 포함) 모두 기준 — 실패가 반복돼도
    # usage·토큰 엔드포인트를 클릭마다 다시 때리지 않는다
    last = max(t for t in (s.get("usageFetchedAt"), s.get("manualRefreshAt"), 0)
               if isinstance(t, (int, float)))
    if now - last < MANUAL_REFRESH_MIN_S:
        failed = last > (s.get("usageFetchedAt") or 0)
        return ("recent-failed" if failed else "recent"), last
    until = (s.get("fetchBackoff") or {}).get("until") or 0
    if now < until:
        # 페널티 윈도우 재점화 방지 — 수동 요청이라도 백오프는 지킨다
        return "backoff", until
    if n != st.get("activeSlot"):
        maybe_release_quarantine(st, n)
    s["manualRefreshAt"] = now
    oauth, err = slot_access_oauth(st, n, allow_refresh=True)
    if not oauth:
        return "error", err
    usage, err, retry_after = usage_api.fetch_usage(oauth["accessToken"])
    if record_usage_fetch(st, n, usage, err, retry_after, now):
        return "ok", None
    if err == "http-429":
        return "backoff", (s.get("fetchBackoff") or {}).get("until")
    return "error", err


_REFRESH_ERR_KEYS = {
    "unmanaged": "refresh_unmanaged", "token-expired": "refresh_token_expired",
    "no-credential": "refresh_no_credential", "keychain": "refresh_keychain",
    "keychain-write": "refresh_keychain_write", "quarantined": "refresh_quarantined",
    "refresh-transient": "refresh_transient",
}


def refresh_message(n: int, status: str, detail, lang: str = "ko") -> str:
    """refresh_slot_usage 결과의 사용자 표시 문구(en|ko)."""
    tl = i18n.tl
    if status == "ok":
        return tl(lang, "refresh_ok", n=n)
    if status == "recent":
        return tl(lang, "refresh_recent", n=n)
    if status == "recent-failed":
        return tl(lang, "refresh_recent_failed", n=n, secs=f"{MANUAL_REFRESH_MIN_S:.0f}")
    if status == "backoff":
        mins = max(1, round(((detail or 0) - time.time()) / 60))
        return tl(lang, "refresh_backoff", n=n, mins=mins)
    if status == "skip":
        return tl(lang, "refresh_skip", n=n)
    key = _REFRESH_ERR_KEYS.get(detail)
    text = tl(lang, key) if key else tl(lang, "refresh_fetch_failed", detail=detail)
    return tl(lang, "refresh_with", n=n, text=text)


def quarantine(st: dict, n: int, reason: str) -> None:
    s = state.slot_of(st, n)
    if not s or s.get("quarantine"):
        return
    blob = None
    try:
        blob = creds.kc_read_slot(n)
    except KeychainError:
        pass
    s["quarantine"] = {
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
        "refreshTokenFingerprint": oauth_mod.refresh_fingerprint(blob) if blob else None,
    }
    logutil.log(i18n.t("log_quarantine", n=n, email=s.get("email"), reason=reason))
    notify.notify_once(st, f"quarantine-{n}", i18n.t("notify_quarantine_title"),
                       i18n.t("notify_quarantine_body", n=n, email=s.get("email")))
