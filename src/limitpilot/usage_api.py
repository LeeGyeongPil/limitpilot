"""Usage source: the Claude usage endpoint, or an injected source (practice mode, tests).

``UsageSource`` is the interface: ``fetch(access_token)`` → (normalized usage | None,
error kind | None, retry-after seconds | None), ``fetch_raw(access_token)`` → (raw json |
None, error kind | None), ``refresh(blob)`` → (new credential blob | None, outcome).
A source may also offer ``keepalive(access_token)`` → (ok, error kind | None, detail | None)
to stand in for the one-token keep-alive message (practice mode does).
``set_source()`` swaps the network for another source; the decision code is unchanged.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from limitpilot.constants import (ANTHROPIC_VERSION, BETA_HEADER, KEEPALIVE_INTERVAL_S,
                                  KEEPALIVE_MODEL, KEEPALIVE_RETRY_S, KEEPALIVE_SYSTEM,
                                  KEEPALIVE_TIMEOUT_S, MESSAGES_URL, USAGE_URL, USER_AGENT)
from limitpilot.policy import limits as limits_mod
from limitpilot import sandbox

_source = None   # None = 실제 HTTP. 연습 모드·테스트가 시나리오 소스로 바꾼다


class UsageSource:
    """Interface for usage / token sources (see module docstring)."""

    def fetch(self, access_token: str):
        raise NotImplementedError

    def fetch_raw(self, access_token: str):
        raise NotImplementedError

    def refresh(self, blob: str):
        raise NotImplementedError


def set_source(source) -> None:
    global _source
    _source = source


def source():
    return _source


def fetch_usage(access_token: str):
    """Usage request → (normalized | None, err_kind | None, retry_after_s | None).

    retry_after_s comes from a 429's Retry-After header and wins over the back-off
    schedule."""
    if _source is not None:
        return _source.fetch(access_token)
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": BETA_HEADER,
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        retry_after = None
        try:
            retry_after = float(int(e.headers.get("Retry-After")))
        except (TypeError, ValueError):
            pass  # 없거나 HTTP-date 형식 — 지수 스케줄로 폴백
        return None, f"http-{e.code}", retry_after
    except (urllib.error.URLError, TimeoutError):
        return None, "network", None
    except json.JSONDecodeError:
        return None, "bad-response", None
    return normalize_usage(raw), None, None


def _money_dollars(m):
    """usage 응답의 금액 객체({amount_minor, exponent}) → 달러(float). 불명이면 None."""
    if not isinstance(m, dict):
        return None
    amount = m.get("amount_minor")
    exponent = m.get("exponent")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return None
    if not isinstance(exponent, int) or isinstance(exponent, bool) or exponent < 0:
        return None
    return amount / (10 ** exponent)


def spend_from_usage_raw(raw: dict):
    """엔터프라이즈 월 달러 소진 정보 추출 → {used, limit, currency, pct} | None.

    구독 계정은 spend.enabled=false·limit=null이라 None을 돌려주고, 엔터프라이즈
    (usage-based)만 사용 금액/한도를 반환한다. pct는 응답의 정수 반올림값
    (소액에서 0으로 뭉개짐) 대신 used/limit로 직접 계산해 달러 표시와 일치시킨다."""
    spend = (raw or {}).get("spend")
    if not isinstance(spend, dict) or not spend.get("enabled"):
        return None
    used = _money_dollars(spend.get("used"))
    limit = _money_dollars(spend.get("limit"))
    if used is None or limit is None or limit <= 0:
        return None
    currency = (spend.get("used") or {}).get("currency") or "USD"
    return {"used": round(used, 2), "limit": round(limit, 2),
            "currency": currency, "pct": round(used / limit * 100.0, 2)}


def normalize_usage(raw: dict):
    """Usage API 응답을 자동전환 결정용 5h/7d 창으로 정규화한다.

    최신 응답의 ``limits[]``를 우선하고, 누락된 창은 구형
    ``five_hour``/``seven_day`` 필드에서 채운다. 대시보드와 데몬이 서로 다른
    응답 필드를 읽으면 화면에서는 임계치에 도달했는데 자동전환은 보류될 수 있다.
    표시용 창 목록(``windows``, 모델별 주간 포함)도 함께 보존해 대시보드가
    자체 API 조회 없이 이 스냅샷만으로 전체 창을 그릴 수 있게 한다.
    엔터프라이즈 계정은 세션/주간 창 없이 ``spend``(월 달러 한도)만 온다 — 이를
    보존해 화면·케이던스가 달러 소진율로 동작하게 한다.
    """
    if not isinstance(raw, dict):
        return None

    def legacy_win(key):
        w = raw.get(key)
        if not isinstance(w, dict) or not isinstance(w.get("utilization"), (int, float)):
            return None
        return {"pct": float(w["utilization"]), "resets_at": w.get("resets_at")}

    out = {"five_hour": None, "seven_day": None, "windows": [],
           "spend": spend_from_usage_raw(raw)}
    limit_keys = {"session": "five_hour", "weekly_all": "seven_day"}
    limits = raw.get("limits")
    if isinstance(limits, list):
        for limit in limits:
            if not isinstance(limit, dict):
                continue
            kind = limit.get("kind")
            pct = limit.get("percent")
            if not isinstance(pct, (int, float)):
                continue
            key = limit_keys.get(kind)
            if key is not None:
                out[key] = {"pct": float(pct), "resets_at": limit.get("resets_at")}
                out["windows"].append({"kind": kind, "pct": float(pct),
                                       "resets_at": limit.get("resets_at")})
            elif kind == "weekly_scoped":
                model = ((limit.get("scope") or {}).get("model") or {}).get("display_name")
                if model:
                    out["windows"].append({"kind": kind, "model": model,
                                           "pct": float(pct),
                                           "resets_at": limit.get("resets_at")})

    if out["five_hour"] is None:
        out["five_hour"] = legacy_win("five_hour")
    if out["seven_day"] is None:
        out["seven_day"] = legacy_win("seven_day")
    if out["five_hour"] is None and out["seven_day"] is None and out["spend"] is None:
        # 세션/주간도 달러 한도도 못 읽으면 사용량 불명 — 스냅샷을 오염시키지 않는다.
        # 엔터프라이즈는 spend가 있어 여기서 걸러지지 않는다.
        return None
    if not out["windows"]:
        for key, kind in (("five_hour", "session"), ("seven_day", "weekly_all")):
            if out[key]:
                out["windows"].append({"kind": kind, **out[key]})
    return out


def send_keepalive(access_token: str):
    """최소 메시지 1건(max_tokens=1) 전송 → (ok, err | None, detail | None).

    detail은 API 오류 응답의 message(진단용, 160자)뿐이다 — 요청 헤더·토큰은
    결과·로그 어디에도 싣지 않는다. 응답 본문은 읽고 버린다."""
    stand_in = getattr(_source, "keepalive", None)
    if stand_in is not None:
        return stand_in(access_token)   # 연습 모드: 보내지 않고 시나리오가 답한다
    body = json.dumps({
        "model": KEEPALIVE_MODEL, "max_tokens": 1, "system": KEEPALIVE_SYSTEM,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    req = urllib.request.Request(MESSAGES_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {access_token}",
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-beta": BETA_HEADER,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=KEEPALIVE_TIMEOUT_S) as resp:
            resp.read(65536)
        return True, None, None
    except urllib.error.HTTPError as e:
        detail = None
        try:
            err_body = json.loads(e.read(8192).decode(errors="replace"))
            msg = ((err_body or {}).get("error") or {}).get("message")
            if isinstance(msg, str) and msg:
                detail = msg[:160]
        except (OSError, ValueError, AttributeError):
            pass
        finally:
            e.close()  # 응답 소켓 해제 — GC에 기대지 않는다
        return False, f"http-{e.code}", detail
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, "network", None
    except sandbox.SandboxViolation:
        # 샌드박스가 막은 진짜 요청은 '계정의 유지 호출 실패'가 아니다 — 기록하지 않고 그대로 알린다
        raise
    except Exception:
        # 헤더 검증 예외 등은 메시지에 토큰이 실릴 수 있다 — 종류만 돌려준다
        return False, "error", None


def keepalive_eligible(s) -> bool:
    """유지 호출 대상 — 구독 OAuth 계정만(OFF 포함). 게이트웨이·엔터프라이즈(종량
    과금)·제한 토큰·격리 슬롯은 제외."""
    return bool(s) and not limits_mod.is_manual_only_slot(s) and s.get("kind") != "setup-token" \
        and not s.get("quarantine")


def keepalive_due(s, now: float) -> bool:
    """마지막 유지 호출 이후 하루(일시 실패였다면 1시간)가 지났는지."""
    ka = (s or {}).get("keepalive") or {}
    at = ka.get("at")
    if not isinstance(at, (int, float)):
        return True
    wait = KEEPALIVE_RETRY_S if ka.get("retry") else KEEPALIVE_INTERVAL_S
    return now - at >= wait
