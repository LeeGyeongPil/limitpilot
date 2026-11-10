"""Status payload for the dashboard (/api/status).

The daemon keeps usage snapshots fresh; the dashboard only falls back to a live usage
request (and only then reads the credential store) when a snapshot is stale.
"""

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from limitpilot.constants import (BETA_HEADER, FINAL_BURN_S, SESSION_BURN_S, THRESHOLD_MAX, THRESHOLD_MIN, USAGE_URL, USER_AGENT)
from limitpilot.errors import (KeychainError)
from limitpilot import accounts
from limitpilot.dashboard import server
from limitpilot import gateway
from limitpilot import gateway_usage
from limitpilot import i18n
from limitpilot import locks
from limitpilot import oauth as oauth_mod
from limitpilot import paths
from limitpilot.policy import limits
from limitpilot import state
from limitpilot.store import creds
from limitpilot.store import filestore
from limitpilot import usage_api


SNAPSHOT_FRESH_S = 660.0  # 데몬 스냅샷을 신선으로 보는 한계 — 데몬 실측 주기(활성
                          # ACTIVE_PROBE_S 180초, 비활성 CANDIDATE_PROBE_S 600초)보다
                          # 약간 크게. 신선하면 대시보드는 usage API 네트워크 조회만
                          # 생략한다(신원·만료 검사는 로컬이라 항상 수행) — usage API
                          # 호출이 데몬의 예산 안에서만 나가게 유지된다.
CACHE_TTL_S = 55.0  # 데몬 부재/스냅샷 노후 시 폴백 라이브 조회의 서버측 캐시.
                    # 표시 폴링(refreshSec, 최소 5초)이 아무리 짧아도 라이브 조회는
                    # 이 캐시가 계정당 분당 1회로 상한한다. 다중 탭도 캐시가 흡수.
ERROR_BACKOFF_S = 270.0  # 폴백 조회 실패(429 등) 후 재시도 간격 — 실패를 55초마다
                         # 재타격하면 429 폭주를 대시보드가 스스로 연장하게 된다.

WINDOW_LABELS = {
    "ko": {"session": "세션 (5시간)", "weekly_all": "주간 (7일)", "weekly_scoped": "주간"},
    "en": {"session": "Session (5h)", "weekly_all": "Weekly (7d)", "weekly_scoped": "Weekly"},
}


def tr(lang: str, ko: str, en: str) -> str:
    """UI 언어(settings.language)에 따른 문자열 선택 — 위젯의 L()과 동일 규약."""
    return en if lang == "en" else ko


def _lang() -> str:
    """현재 UI 언어 — state를 아직 들고 있지 않은 경로용(로컬 파일 1회 읽기)."""
    try:
        return state.get_settings(state.load_state())["language"]
    except Exception:
        return i18n.default_lang()

# 대시보드 서버는 게이트웨이 집계를 백그라운드 스레드로 돌린다(server.main 이 켠다)
_BACKGROUND_SCAN = False

_cache_lock = threading.Lock()
_cache = {}  # slot -> (fetched_at, windows|None, spend|None, err|None)

# (slot, live?) -> 토큰 만료 시각(ms). 슬롯 수만큼만 커진다(상한 = 등록 슬롯 × 2).
_expiry_ms = {}


def _remember_expiry(n: int, live: bool, expires_at_ms) -> None:
    if len(_expiry_ms) > 256:
        _expiry_ms.clear()  # 비정상적으로 많아지면 비운다(상한 없는 캐시 금지)
    _expiry_ms[(n, live)] = float(expires_at_ms)


def status_etag():
    """ETag for /api/status from state.json (mtime, size), the live cache and the clock.

    The 30-second time bucket keeps relative labels ("updated 2 min ago") moving while
    a hidden or idle page re-polls without the server rebuilding the whole status.
    """

    try:
        info = os.stat(paths.state_file())
        key = f"{info.st_mtime_ns}-{info.st_size}"
    except OSError:
        key = "none"
    with _cache_lock:
        gen = ",".join(f"{k}:{v[0]:.0f}" for k, v in sorted(_cache.items()))
    raw = f"{key}|{gen}|{int(time.time() // 30)}|{server.BUILD_ID}"

    return '"' + hashlib.sha1(raw.encode()).hexdigest()[:16] + '"'


def fetch_raw_usage(access_token: str):
    """usage 원본 JSON 조회 → (raw | None, err_kind | None). 읽기 전용."""
    if usage_api.source() is not None:
        return usage_api.source().fetch_raw(access_token)   # 연습 모드 시나리오
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": BETA_HEADER,
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, f"http-{e.code}"
    except json.JSONDecodeError:
        return None, "bad-response"
    except Exception:
        # ValueError 등 헤더 검증 예외는 메시지에 토큰이 포함될 수 있음 —
        # 어떤 예외도 이 함수 밖으로 내보내지 않는다(무누출 계약)
        return None, "fetch-error"


def windows_from_raw(raw: dict, lang: str = "ko"):
    """usage 응답 → 표시용 창 목록. limits[] 우선(모델별 주간 포함), 없으면
    five_hour/seven_day 폴백."""
    labels = WINDOW_LABELS["en" if lang == "en" else "ko"]
    wins = []
    for lim in raw.get("limits") or []:
        if not isinstance(lim, dict) or not isinstance(lim.get("percent"), (int, float)):
            continue
        kind = lim.get("kind")
        if kind in ("session", "weekly_all"):
            label = labels[kind]
        elif kind == "weekly_scoped":
            model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
            if not model:
                continue
            label = f"{labels['weekly_scoped']} · {model}"
        else:
            continue
        wins.append({"kind": kind, "label": label,
                     "pct": float(lim["percent"]), "resetsAt": lim.get("resets_at")})
    if wins:
        return wins
    for key, kind in (("five_hour", "session"), ("seven_day", "weekly_all")):
        w = raw.get(key)
        if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
            wins.append({"kind": kind, "label": labels[kind],
                         "pct": float(w["utilization"]), "resetsAt": w.get("resets_at")})
    return wins


def windows_from_snapshot(usage: dict, lang: str = "ko"):
    """state.json lastGood 스냅샷 → 표시용 창 목록. 신형 스냅샷은 데몬이 보존한
    windows(모델별 주간 포함)를 우선하고, 구형은 five_hour/seven_day 폴백."""
    labels = WINDOW_LABELS["en" if lang == "en" else "ko"]
    wins = []
    for w in (usage or {}).get("windows") or []:
        if not isinstance(w, dict) or not isinstance(w.get("pct"), (int, float)):
            continue
        kind = w.get("kind")
        if kind in ("session", "weekly_all"):
            label = labels[kind]
        elif kind == "weekly_scoped" and w.get("model"):
            label = f"{labels['weekly_scoped']} · {w['model']}"
        else:
            continue
        wins.append({"kind": kind, "label": label,
                     "pct": float(w["pct"]), "resetsAt": w.get("resets_at")})
    if wins:
        return wins
    for key, kind in (("five_hour", "session"), ("seven_day", "weekly_all")):
        w = (usage or {}).get(key)
        if isinstance(w, dict) and isinstance(w.get("pct"), (int, float)):
            wins.append({"kind": kind, "label": labels[kind],
                         "pct": float(w["pct"]), "resetsAt": w.get("resets_at")})
    return wins


def spend_from_snapshot(usage: dict):
    """state.json lastGood 스냅샷 → 엔터프라이즈 달러 소진 정보(정규화 시 보존됨)."""
    return (usage or {}).get("spend")


def gateway_usage_meter(slot: dict, usage: dict) -> dict:
    """Build token and optional USD meters without exposing gateway credentials."""
    raw_config = (slot or {}).get("gatewayUsageConfig") or {}
    config = gateway.validate_gateway_usage_config(*[
        raw_config.get(key, 0) for key in (
            "inputTokenLimit", "outputTokenLimit", "inputPricePerMillion",
            "outputPricePerMillion", "costLimit")])
    input_used = sum(int((usage or {}).get(key, 0) or 0)
                     for key in ("input", "cacheRead", "cacheCreation"))
    output_used = int((usage or {}).get("output", 0) or 0)
    input_cost = input_used * config["inputPricePerMillion"] / 1_000_000
    output_cost = output_used * config["outputPricePerMillion"] / 1_000_000
    cost_used = input_cost + output_cost

    def meter(used, limit):
        return {"used": used, "limit": limit,
                "pct": used / limit * 100 if limit > 0 else None}

    return {
        "input": meter(input_used, config["inputTokenLimit"]),
        "output": meter(output_used, config["outputTokenLimit"]),
        "cost": {**meter(cost_used, config["costLimit"]),
                 "input": input_cost, "output": output_cost,
                 "enabled": config["inputPricePerMillion"] > 0
                            or config["outputPricePerMillion"] > 0},
        "config": config,
    }


def _keepalive_view(s: dict):
    """유지 호출 기록의 표시용 사본 — 알려진 필드만(값은 시각·성공 여부·오류 종류)."""
    ka = (s or {}).get("keepalive")
    if not isinstance(ka, dict) or not isinstance(ka.get("at"), (int, float)):
        return None
    return {"at": ka["at"], "ok": bool(ka.get("ok")), "err": ka.get("err"),
            "note": ka.get("note"), "detail": ka.get("detail")}


def slot_status(st: dict, n: int, active: bool, lang: str = "ko"):
    """슬롯 하나의 표시 데이터 구성. 절대 쓰기 없음 — 만료 토큰은 리프레시하지
    않고 스냅샷 폴백으로만 표시한다."""
    s = state.slot_of(st, n)
    if not s:
        return {"slot": n, "registered": False}
    out = {
        "slot": n, "registered": True, "active": active,
        "label": state.slot_label(s, n),
        "alias": s.get("alias"), "email": s.get("email"), "orgName": s.get("orgName"),
        "kind": s.get("kind"),
        "enterprise": limits.is_enterprise_slot(s),
        "gateway": limits.is_gateway_slot(s),
        "modelId": s.get("modelId"), "baseUrl": s.get("baseUrl"),
        "authMode": s.get("authMode") if limits.is_gateway_slot(s) else None,
        "tokenUsage": None,
        "gatewayUsage": None,
        "enabled": s.get("enabled", True),
        "quarantine": s.get("quarantine"),
        "tokenExpired": False, "error": None,
        "windows": [], "spend": None, "fetchedAt": None,
        "weeklyCap": limits.weekly_cap(s),
        "limits": limits.slot_limits(st, s, s.get("usage")),
        "keepalive": _keepalive_view(s),
    }

    def snapshot():
        # 스냅샷 폴백 — 세션/주간 창과 엔터프라이즈 달러 한도를 함께 채운다
        out["windows"] = windows_from_snapshot(s.get("usage"), lang)
        out["spend"] = spend_from_snapshot(s.get("usage"))
        out["fetchedAt"] = s.get("usageFetchedAt")
        return out

    if s.get("kind") == "setup-token":
        # 구버전에서 등록한 inference-only 토큰은 profile/usage 권한과 refresh
        # 자격증명이 없다. 사용량 API를 호출하지 않고 재등록 대상으로만 표시한다.
        out["error"] = "limited-token"
        return out
    if limits.is_gateway_slot(s):
        # The token is deliberately not read or returned. Gateway profiles have
        # no Anthropic subscription usage endpoint and are manual-only.
        out["tokenUsage"] = gateway_usage.gateway_token_usage(s, background=_BACKGROUND_SCAN)
        out["gatewayUsage"] = gateway_usage_meter(s, out["tokenUsage"])
        return out
    if not out["enabled"]:
        # OFF 계정은 조회하지 않음 — 마지막 스냅샷만 표시
        return snapshot()
    if s.get("quarantine"):
        return snapshot()
    if limits.exhausted_until(s) or limits.capped_until(s, time.time(), st):
        # 소진 확정 계정은 리셋 전까지 API가 429를 반환 — 조회 생략, 스냅샷이 진실.
        # 주간 상한 도달 계정도 데몬이 리셋까지 조회를 쉬므로 대시보드가 대신 때리지 않는다
        return snapshot()

    # 활성 슬롯은 라이브 키체인이 진실이지만, 데몬의 전환이 진행 중이면 키체인은
    # 이미 상대 계정으로 바뀌었는데 state 스냅샷은 옛 activeSlot일 수 있다 —
    # ~/.claude.json의 현재 로그인 email과 슬롯 email이 일치할 때만 라이브를 신뢰
    use_live = False
    live_slot = None
    if active:
        try:
            live_oauth = filestore.read_json(paths.CLAUDE_JSON).get("oauthAccount") or {}
            live_slot = accounts.find_slot_by_identity(st, live_oauth)
            use_live = live_slot == n
        except (OSError, json.JSONDecodeError):
            use_live = False
        if not use_live:
            out["error"] = "unmanaged" if live_slot is None else "switching"
            return snapshot()

    # 성능: 아래 세 경우는 자격증명 저장소(키체인)를 읽지 않는다 — 5초 폴링마다 슬롯별
    # `security` 프로세스를 띄우던 문제(키체인 확인창 유발)를 없앤다. 토큰 만료 여부는
    # 마지막으로 읽었을 때 기억해 둔 만료 시각으로 판단한다.
    now = time.time()
    # 데몬이 기록한 만료 시각(비밀 아님) → 없으면 이 서버가 마지막으로 읽은 값
    remembered = s.get("tokenExpiresAt")
    if not isinstance(remembered, (int, float)) or isinstance(remembered, bool):
        remembered = _expiry_ms.get((n, use_live))
    if remembered is not None:
        # 활성 토큰은 Claude Code 가 쓰는 기준(5분 여유)과 같은 판정
        if oauth_mod.is_expired({"expiresAt": remembered}):
            out["tokenExpired"] = True
    # (1) 데몬이 방금 갱신한 스냅샷이 신선하면 그대로 쓴다(usage API 호출도 없음)
    fetched = s.get("usageFetchedAt")
    if fetched and now - fetched <= SNAPSHOT_FRESH_S:
        return snapshot()
    # (2) 데몬이 429 백오프 중이면 대시보드가 대신 때리지 않는다(페널티 재점화 방지)
    backoff = s.get("fetchBackoff") or {}
    if now < (backoff.get("until") or 0):
        return snapshot()
    # (3) 직전 폴백 조회 결과가 캐시 TTL 안이면 그 값을 쓴다.
    # 실패 항목은 성공(CACHE_TTL_S)보다 오래 눌러 둔다 — 429 폭주 구간 백오프.
    with _cache_lock:
        cached = _cache.get(n)
    ttl = CACHE_TTL_S if (cached and cached[3] is None) else ERROR_BACKOFF_S
    if cached and now - cached[0] <= ttl:
        if cached[3] is None:
            out["windows"], out["spend"], out["fetchedAt"] = cached[1], cached[2], cached[0]
            return out
        out["error"] = cached[3]
        return snapshot()

    # 스냅샷이 낡았다(데몬 부재·조회 실패) — 이때만 저장소를 읽고 폴백 라이브 조회
    blob = None
    try:
        blob = creds.kc_read_active() if use_live else creds.kc_read_slot(n)
    except KeychainError:
        out["error"] = "keychain"
    oauth = oauth_mod.parse_oauth(blob) if blob else None
    if oauth and isinstance(oauth.get("expiresAt"), (int, float)):
        _remember_expiry(n, use_live, oauth["expiresAt"])
    if oauth and not oauth_mod.is_expired(oauth):
        out.pop("tokenExpired", None)
        raw, err = fetch_raw_usage(oauth["accessToken"])
        if raw:
            wins = windows_from_raw(raw, lang)
            spend = usage_api.spend_from_usage_raw(raw)
            now = time.time()
            with _cache_lock:
                _cache[n] = (now, wins, spend, None)
            out["windows"], out["spend"], out["fetchedAt"] = wins, spend, now
            return out
        with _cache_lock:
            _cache[n] = (time.time(), None, None, err)
        out["error"] = err
    elif oauth:
        out["tokenExpired"] = True
    elif not out["error"]:
        out["error"] = "no-credential"

    # 라이브 조회 불가 — 데몬이 남긴 마지막 스냅샷으로 폴백(오래됨 표시는 프론트 몫)
    return snapshot()


def build_status():
    st = state.load_state()
    # state.json 스냅샷이 늦더라도 기본 Claude 프로필의 실제 UUID 결합을 표시 진실로 쓴다.
    live_binding = accounts.reconcile_active_slot(st)
    active = st.get("activeSlot")
    settings = state.get_settings(st)
    lang = settings["language"]
    slots = [slot_status(st, n, n == active, lang) for n in state.registered_slots(st)]
    # Persist numeric high-water marks so deleting/compacting old transcript files
    # never makes a gateway's cumulative total move backwards.
    updates = []
    for item in slots:
        if item.get("gateway") and isinstance(item.get("tokenUsage"), dict):
            source = state.slot_of(st, item["slot"]) or {}
            if item["tokenUsage"].get("total", 0) > (source.get("tokenUsage") or {}).get("total", 0):
                updates.append((item["slot"], source.get("modelId"),
                                source.get("addedAt"), item["tokenUsage"]))
    if updates:
        with locks.state_lock():
            latest = state.load_state()
            changed = False
            for n, model, added_at, usage in updates:
                target = state.slot_of(latest, n)
                if (limits.is_gateway_slot(target) and target.get("modelId") == model
                        and target.get("addedAt") == added_at
                        and usage.get("total", 0) > (target.get("tokenUsage") or {}).get("total", 0)):
                    target["tokenUsage"] = dict(usage)
                    changed = True
            if changed:
                state.save_state(latest)
    # 마지막에 빈 슬롯 카드 1장 — 계정 추가 안내용 (등록 수 제한 없음)
    slots.append({"slot": state.next_free_slot(st), "registered": False})
    return {
        "generatedAt": time.time(),
        "build": server.BUILD_ID,
        "activeSlot": active,
        "liveStatus": live_binding["status"],
        "lastSwitchAt": st.get("lastSwitchAt"),
        "threshold": settings["threshold"],
        "weeklyThreshold": settings["weeklyThreshold"],
        "weeklyThresholdSet": settings["weeklyThresholdSet"],
        "language": lang,
        "keepalive": settings["keepalive"],
        "autoSwitch": settings["autoSwitch"],
        # 온보딩 체크리스트용: 데몬 사본이 있으면 설치된 것으로 본다(파일 존재만 확인)
        "daemonInstalled": os.path.exists(os.path.join(paths.daemon_dir(), "limitpilot.pyz")),
        "demo": os.environ.get("LIMITPILOT_DEMO") == "1",
        "priority": settings["priority"],
        "finalBurn": settings["finalBurn"],
        "finalBurnHours": FINAL_BURN_S / 3600,
        "sessionBurnMinutes": SESSION_BURN_S / 60,
        "thresholdMin": THRESHOLD_MIN,
        "thresholdMax": THRESHOLD_MAX,
        "slots": slots,
    }
