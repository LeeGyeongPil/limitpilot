"""Local web dashboard: ``limitpilot dashboard`` → http://127.0.0.1:8748.

Security model (the server only listens on 127.0.0.1):

* every run creates a random API token; it is written into the served HTML and every
  ``/api/*`` request must send it back (``X-LimitPilot-Token``),
* the ``Host`` header must be ``127.0.0.1:<port>`` or ``localhost:<port>`` (blocks DNS
  rebinding) and a present ``Origin`` must be the same address (blocks other sites),
* ``/api/health`` (no secrets) and ``/api/shutdown`` (needs the per-run nonce from the
  0600 runtime record) are the only token-free endpoints.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from limitpilot.i18n import t as _t
from limitpilot import __version__ as APP_VERSION
from limitpilot.constants import WEEKLY_CAP_MAX
from limitpilot.errors import LimitPilotError
from limitpilot import accounts
from limitpilot import collector
from limitpilot.daemon import deploy
from limitpilot.dashboard import runtime
from limitpilot.dashboard import status as status_mod
from limitpilot import gateway
from limitpilot import i18n
from limitpilot import locks
from limitpilot.login import session
from limitpilot import paths
from limitpilot.policy import limits
from limitpilot import sandbox
from limitpilot import state
from limitpilot.store import creds
from limitpilot.store import filestore
from limitpilot import switch


DEFAULT_PORT = paths.DASHBOARD_DEFAULT_PORT
APP_ID = paths.DASHBOARD_APP_ID
TOKEN_HEADER = "X-LimitPilot-Token"


def read_html() -> bytes:
    """The single static page, read through importlib.resources (works inside a .pyz)."""
    return resources.files("limitpilot.dashboard").joinpath("static/dashboard.html").read_bytes()


def _compute_build_id() -> str:
    """Content fingerprint of the package code and the page.

    The version number can stay the same between builds, so it cannot tell whether a
    running dashboard is current. When the fingerprint differs the old server is
    replaced and open pages reload themselves.
    """
    digest = hashlib.sha256()
    try:
        digest.update(deploy.source_fingerprint().encode())
    except OSError:
        digest.update(b"no-source")
    try:
        digest.update(read_html())
    except OSError:
        digest.update(b"missing:dashboard.html")
    return digest.hexdigest()[:16]


BUILD_ID = _compute_build_id()
BUILD_PLACEHOLDER = b"__DASHBOARD_BUILD__"
TOKEN_PLACEHOLDER = b"__DASHBOARD_TOKEN__"
DEMO_PLACEHOLDER = b"__DASHBOARD_DEMO__"
STALE_STOP_TIMEOUT_S = 5.0     # 이전 빌드 서버 종료 대기
REOPEN_WAIT_S = 8.0            # 교체 후 기존 창이 새로고침해 다시 붙기를 기다리는 시간

# UI 창 프레즌스 — /api/presence SSE 연결 수(= 열려 있는 현재 빌드 대시보드 창 수).
# 이전 빌드 페이지(빌드 파라미터 없음/불일치)는 세지 않는다 — 그 창은 새 UI를 못
# 그리므로 '이미 열린 창'으로 취급하면 사용자가 예전 화면에 갇힌다.
_presence_lock = threading.Lock()
_presence_count = 0

# 브라우저 로그인 추가 — 격리된 Claude OAuth PTY, 백그라운드 워커가 인증 완료 감지
_add_lock = threading.Lock()
_add_session = None   # ca.ClaudeLoginSession | None
_add_status = {"status": "idle"}  # idle|running|done|error (+url/slot/message)
_shutdown_requested = threading.Event()


def _add_worker(sess, alias, session_id):
    """브라우저 OAuth 완료를 대기해 전체 자격증명과 계정 정보를 슬롯에 등록한다."""
    global _add_session
    try:
        blob, oauth_account = sess.wait_for_credentials()
        with _add_lock:
            if _add_session is not sess or _shutdown_requested.is_set():
                return
            with locks.state_lock():
                st = state.load_state()
                n, label, is_new = accounts.register_oauth_slot(
                    st, blob, oauth_account, alias=alias
                )
            lang = state.get_settings(st)["language"]
            with status_mod._cache_lock:
                status_mod._cache.pop(n, None)
            _add_status.clear()
            _add_status.update({
                "status": "done", "slot": n, "sessionId": session_id,
                "message": status_mod.tr(lang, f"슬롯 {n}: {label} {'등록' if is_new else '갱신'}",
                              f"Slot {n}: {label} {'registered' if is_new else 'updated'}")})
            _add_session = None
    except LimitPilotError as e:
        with _add_lock:
            if _add_session is sess:
                _add_status.clear()
                _add_status.update({"status": "error", "sessionId": session_id,
                                    "message": str(e)})
                _add_session = None
    except Exception as e:
        print(i18n.t("http_error_log", where="add worker", err=type(e).__name__), file=sys.stderr)
        with _add_lock:
            if _add_session is sess:
                _add_status.clear()
                _add_status.update({"status": "error", "sessionId": session_id,
                                    "message": status_mod.tr(status_mod._lang(), "내부 오류", "Internal error")})
                _add_session = None
    finally:
        sess.close()


def start_add(alias):
    """격리된 Claude OAuth 로그인 PTY와 자격증명 등록 워커를 시작한다.
    반환 {url}. 로그인 완료 후 자격증명은 서버 내부에서만 슬롯에 저장된다."""
    global _add_session
    lang = status_mod._lang()
    if _demo_mode():
        # 연습 모드: 진짜 claude 로그인을 띄우지 않는다 — PTY·실행 오류처럼 보이지 않게 이유를 말한다
        raise LimitPilotError(i18n.tl(lang, "practice_login_dashboard"))
    alias = (alias or "").strip() or None
    if alias and len(alias) > 80:
        raise LimitPilotError(status_mod.tr(lang, "별칭은 80자 이내로 입력하세요",
                                  "Alias must be 80 characters or fewer"))
    with _add_lock:
        if _shutdown_requested.is_set():
            raise LimitPilotError(status_mod.tr(
                lang, "대시보드가 종료 중입니다",
                "The dashboard is shutting down",
            ))
        if _add_session is not None:
            _add_session.close()  # 이전 미완 세션 폐기(supersede)
        sess = session.ClaudeLoginSession()
        try:
            info = sess.start()
        except Exception:
            sess.close()
            raise
        session_id = secrets.token_urlsafe(12)
        _add_session = sess
        _add_status.clear()
        _add_status.update({"status": "running", "sessionId": session_id,
                            "message": status_mod.tr(lang, "브라우저 로그인 대기 중",
                                          "Waiting for browser login")})
    threading.Thread(target=_add_worker, args=(sess, alias, session_id), daemon=True).start()
    return {"url": info.get("url"), "sessionId": session_id}


def feed_add(code, session_id):
    """(폴백) localhost 콜백이 막혀 코드 프롬프트에 멈춘 경우, 붙여넣은 코드 주입."""
    lang = status_mod._lang()
    with _add_lock:
        sess = _add_session
        if sess is None:
            raise LimitPilotError(status_mod.tr(lang, "진행 중인 로그인 세션이 없습니다",
                                      "No login session in progress"))
        if not session_id or session_id != _add_status.get("sessionId"):
            raise LimitPilotError(status_mod.tr(lang, "로그인 세션이 바뀌었습니다 — 새 화면에서 다시 시도하세요",
                                      "Login session changed — retry from the new panel"))
        sess.feed(code)
        _add_status["message"] = status_mod.tr(lang, "인증 코드 처리 중", "Processing auth code")
    return status_mod.tr(lang, "코드를 전달했습니다", "Code delivered")


def cancel_add(session_id=None, force=False):
    """진행 중인 PTY와 callback listener를 종료하고 워커 소유권을 해제한다."""
    global _add_session
    lang = status_mod._lang()
    with _add_lock:
        if not force and (not session_id or session_id != _add_status.get("sessionId")):
            raise LimitPilotError(status_mod.tr(lang, "로그인 세션이 바뀌어 이전 취소 요청을 무시했습니다",
                                      "Login session changed — stale cancel ignored"))
        sess = _add_session
        _add_session = None
        _add_status.clear()
        _add_status.update({"status": "idle"})
    if sess is not None:
        sess.close()
    return status_mod.tr(lang, "계정 추가를 취소했습니다", "Account add cancelled")


def apply_settings(body: dict) -> str:
    """설정 변경 — ca.set_setting에 범위 검증 위임."""
    with locks.state_lock():
        st = state.load_state()
        lang = state.get_settings(st)["language"]
        changed = []
        if "threshold" in body:
            s = state.set_setting(st, "threshold", body["threshold"])
            changed.append(status_mod.tr(lang, f"세션 임계 {s['threshold']:.0f}%",
                              f"Session threshold {s['threshold']:.0f}%"))
        if "weeklyThreshold" in body:
            value = body["weeklyThreshold"]
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise LimitPilotError(status_mod.tr(lang, "주간 임계는 숫자 또는 null이어야 합니다",
                                          "Weekly threshold must be a number or null"))
            s = state.set_setting(st, "weeklyThreshold", value)
            changed.append(status_mod.tr(lang,
                              f"주간 임계 {s['weeklyThreshold']:.0f}%" if s["weeklyThresholdSet"]
                              else "주간 임계 = 세션 임계",
                              f"Weekly threshold {s['weeklyThreshold']:.0f}%" if s["weeklyThresholdSet"]
                              else "Weekly threshold follows session"))
        if "keepalive" in body:
            if not isinstance(body["keepalive"], bool):
                raise LimitPilotError(status_mod.tr(lang, "keepalive 값은 true 또는 false여야 합니다",
                                          "keepalive must be true or false"))
            s = state.set_setting(st, "keepalive", body["keepalive"])
            changed.append(status_mod.tr(lang, f"유지 호출 {'켜짐' if s['keepalive'] else '꺼짐'}",
                              f"Keep-alive {'on' if s['keepalive'] else 'off'}"))
        if "finalBurn" in body:
            if not isinstance(body["finalBurn"], bool):
                raise LimitPilotError(status_mod.tr(lang, "finalBurn 값은 true 또는 false여야 합니다",
                                          "finalBurn must be true or false"))
            s = state.set_setting(st, "finalBurn", body["finalBurn"])
            changed.append(status_mod.tr(lang, f"리셋 전 소진 {'켜짐' if s['finalBurn'] else '꺼짐'}",
                              f"Use-up before reset {'on' if s['finalBurn'] else 'off'}"))
        if "autoSwitch" in body:
            # 페이지가 확인창을 거친 뒤에만 보낸다(옵트인)
            if not isinstance(body["autoSwitch"], bool):
                raise LimitPilotError(status_mod.tr(lang, "autoSwitch 값은 true 또는 false여야 합니다",
                                                    "autoSwitch must be true or false"))
            s = state.set_setting(st, "autoSwitch", body["autoSwitch"])
            changed.append(status_mod.tr(lang, f"자동 전환 {'켜짐' if s['autoSwitch'] else '꺼짐(알림만)'}",
                                         f"Auto-switch {'on' if s['autoSwitch'] else 'off (notify only)'}"))
        if "priority" in body:
            if not isinstance(body["priority"], str):
                raise LimitPilotError(status_mod.tr(lang, "우선순위 값이 올바르지 않습니다",
                                          "Invalid priority"))
            s = state.set_setting(st, "priority", body["priority"])
            changed.append(status_mod.tr(lang,
                              "리셋 임박 우선" if s["priority"] == "expiring" else "사용률 최저 우선",
                              "Expiring-first" if s["priority"] == "expiring" else "Lowest-usage first"))
    if not changed:
        raise LimitPilotError(status_mod.tr(lang, "변경할 설정이 없습니다", "Nothing to change"))
    return " · ".join(changed)


def mutate_slot(n: int, action: str, force: bool = False) -> str:
    """슬롯 관리 쓰기 — CLI와 동일한 검증(활성 슬롯 보호)을 ca 함수에 위임."""
    with locks.state_lock():
        st = state.load_state()
        lang = state.get_settings(st)["language"]
        if action == "activate":
            slot = state.slot_of(st, n)
            if not slot:
                raise LimitPilotError(status_mod.tr(lang, f"슬롯 {n}은 등록되어 있지 않습니다",
                                          f"Slot {n} is not registered"))
            if slot.get("kind") == "setup-token":
                raise LimitPilotError(status_mod.tr(
                    lang, "제한 토큰은 활성화할 수 없습니다 — 삭제 후 브라우저로 다시 추가하세요",
                    "A limited token cannot be activated — delete it, then re-add via browser"))
            if not slot.get("enabled", True) and not limits.is_manual_only_slot(slot):
                # 엔터프라이즈는 자동전환 대상이 아니라 enabled와 무관하게 수동 전환 허용
                raise LimitPilotError(status_mod.tr(
                    lang, f"슬롯 {n}은 OFF 상태입니다 — 먼저 자동전환 포함으로 켜세요",
                    f"Slot {n} is OFF — turn it on (include in auto-switch) first"))
            if slot.get("quarantine"):
                raise LimitPilotError(status_mod.tr(
                    lang, f"슬롯 {n}은 자격증명 오류로 격리됐습니다 — 브라우저로 다시 로그인하세요",
                    f"Slot {n} is quarantined (credential error) — log in again via browser"))
            gateway_transition = limits.is_gateway_slot(slot) or st.get("gatewayRestore") is not None
            st["manualChoiceAt"] = time.time()  # 사용자 선택 — 리셋 임박 선제 이동 유예
            outcome = switch.perform_switch(st, n, force=force)
            label = state.slot_label(slot, n)
            if outcome == "already":
                if gateway_transition:
                    return status_mod.tr(lang,
                              f"슬롯 {n}({label}) 설정이 활성입니다 — 실행 중인 Claude에는 잠시 후 반영됩니다",
                              f"Slot {n} ({label}) settings are active — running Claude picks them up shortly")
                return status_mod.tr(lang, f"슬롯 {n}({label})이 현재 활성 계정으로 확인됐습니다",
                          f"Slot {n} ({label}) is already the active account")
            if gateway_transition:
                return status_mod.tr(lang,
                          f"슬롯 {n}({label}) 활성화 완료 — 실행 중인 Claude에는 잠시 후 반영됩니다",
                          f"Slot {n} ({label}) activated — running Claude picks it up shortly")
            return status_mod.tr(lang, f"슬롯 {n}({label}) 활성화 완료 — 실행 중인 Claude에는 잠시 후 반영됩니다",
                      f"Slot {n} ({label}) activated — running Claude picks it up shortly")
        if action == "enable":
            email = accounts.set_slot_enabled(st, n, True)
            return status_mod.tr(lang, f"슬롯 {n}({email}) ON", f"Slot {n} ({email}) ON")
        if action == "disable":
            email = accounts.set_slot_enabled(st, n, False)
            return status_mod.tr(lang, f"슬롯 {n}({email}) OFF", f"Slot {n} ({email}) OFF")
        if action == "delete":
            email = accounts.delete_slot(st, n)
            with status_mod._cache_lock:
                status_mod._cache.pop(n, None)
            return status_mod.tr(lang, f"슬롯 {n}({email}) 삭제됨", f"Slot {n} ({email}) deleted")
    raise LimitPilotError(status_mod.tr(status_mod._lang(), "알 수 없는 작업", "Unknown action"))


def refresh_usage(n: int) -> dict:
    """수동 사용량 새로고침(슬롯 1개) — 로그인되지 않은 계정도 토큰을 갱신해 실측한다."""
    with locks.state_lock():
        st = state.load_state()
        lang = state.get_settings(st)["language"]
        accounts.reconcile_active_slot(st)
        status, detail = collector.refresh_slot_usage(st, n)
        state.save_state(st)
    if status == "ok":
        with status_mod._cache_lock:
            status_mod._cache.pop(n, None)  # 폴백 캐시가 방금 실측한 스냅샷을 가리지 않게(실패 백오프는 유지)
    return {"status": status, "message": collector.refresh_message(n, status, detail, lang)}


def refresh_all_usage() -> dict:
    """전체 수동 새로고침 — 슬롯마다 락을 따로 잡아 데몬 틱을 오래 막지 않는다."""
    st = state.load_state()
    lang = state.get_settings(st)["language"]
    results = []
    for n in state.registered_slots(st):
        s = state.slot_of(st, n)
        if limits.is_gateway_slot(s) or (s or {}).get("kind") == "setup-token":
            continue
        try:
            results.append((n, refresh_usage(n)))
        except LimitPilotError:
            continue  # 그 사이 삭제된 슬롯
    ok = sum(1 for _, r in results if r["status"] in ("ok", "recent"))
    failed = [r["message"] for _, r in results if r["status"] not in ("ok", "recent")]
    message = status_mod.tr(lang, f"사용량 새로고침: {ok}/{len(results)}개 계정 갱신",
                 f"Usage refresh: {ok}/{len(results)} accounts updated")
    if failed:
        message += " · " + failed[0] + (status_mod.tr(lang, f" 외 {len(failed) - 1}건", f" (+{len(failed) - 1} more)")
                                         if len(failed) > 1 else "")
    return {"message": message, "updated": ok, "total": len(results)}


def set_limits(n: int, body: dict) -> str:
    """계정별 세션 임계·주간 임계·주간 최대치 — 키가 있으면 변경, null이면 전역값/없음."""
    kw = {}
    for key, arg in (("sessionThreshold", "session"), ("weeklyThreshold", "weekly"),
                     ("weeklyCap", "cap")):
        if key in body:
            value = body[key]
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise LimitPilotError(status_mod.tr(status_mod._lang(), "임계·최대치는 숫자 또는 null이어야 합니다",
                                          "Thresholds and cap must be numbers or null"))
            kw[arg] = value
    with locks.state_lock():
        st = state.load_state()
        lang = state.get_settings(st)["language"]
        lim = limits.set_slot_limits(st, n, **kw)
    return status_mod.tr(lang,
              f"슬롯 {n} 임계 세션 {lim['session']:.0f}% · 주간 {lim['weekly']:.0f}% · 최대 {lim['cap']:.0f}%",
              f"Slot {n} thresholds session {lim['session']:.0f}% · weekly {lim['weekly']:.0f}% · cap {lim['cap']:.0f}%")


def set_weekly_cap(n: int, cap) -> str:
    with locks.state_lock():
        st = state.load_state()
        lang = state.get_settings(st)["language"]
        v = limits.set_slot_weekly_cap(st, n, cap)
    if v >= WEEKLY_CAP_MAX:
        return status_mod.tr(lang, f"슬롯 {n} 주간 상한 해제(100%)", f"Slot {n} weekly cap cleared (100%)")
    return status_mod.tr(lang, f"슬롯 {n} 주간 상한 {v:.0f}%", f"Slot {n} weekly cap {v:.0f}%")


def set_slot_alias(n: int, alias) -> str:
    """표시 별칭만 변경한다. 이메일/OAuth 계정 식별자는 보존한다."""
    if not isinstance(alias, str):
        raise LimitPilotError(status_mod.tr(status_mod._lang(), "별칭은 문자열이어야 합니다",
                                  "Alias must be a string"))
    with locks.state_lock():
        st = state.load_state()
        lang = state.get_settings(st)["language"]
        accounts.set_slot_alias(st, n, alias)
    return status_mod.tr(lang, f"슬롯 {n} 별칭이 {'저장됐습니다' if alias.strip() else '제거됐습니다'}",
              f"Slot {n} alias {'saved' if alias.strip() else 'cleared'}")


def add_gateway(body: dict) -> dict:
    """Verify then persist a third-party gateway. Never returns/logs the token."""
    fields = ("baseUrl", "modelId", "token", "authMode", "alias")
    for key in fields:
        if key in body and body[key] is not None and not isinstance(body[key], str):
            raise LimitPilotError(status_mod.tr(status_mod._lang(), "게이트웨이 입력값이 올바르지 않습니다",
                                      "Invalid gateway input"))
    base_url = body.get("baseUrl") or ""
    model = body.get("modelId") or ""
    token = body.get("token") or ""
    auth_mode = body.get("authMode") or "bearer"
    alias = body.get("alias") or None   # 비우면: 이미 있는 게이트웨이는 예전 별칭, 새것은 모델 이름
    usage_config = gateway.validate_gateway_usage_config(
        body.get("inputTokenLimit", 0), body.get("outputTokenLimit", 0),
        body.get("inputPricePerMillion", 0), body.get("outputPricePerMillion", 0),
        body.get("costLimit", 0))
    # Network verification happens before taking the coarse state/credential lock.
    gateway.probe_gateway_connection(base_url, model, token, auth_mode)
    with locks.state_lock():
        st = state.load_state()
        n, label, is_new = gateway.register_gateway_slot(
            st, base_url, model, token, auth_mode, alias, usage_config)
    lang = status_mod._lang()
    message = status_mod.tr(lang,
                 f"슬롯 {n} 서드파티 게이트웨이 {'등록' if is_new else '갱신'} 완료",
                 f"Third-party gateway {'registered' if is_new else 'updated'} in slot {n}")
    return {"slot": n, "label": label, "message": message}


def edit_gateway(n: int, body: dict) -> dict:
    """Verify and update a gateway. Blank token means keep the Keychain value."""
    fields = ("baseUrl", "modelId", "token", "authMode", "alias")
    for key in fields:
        if key in body and body[key] is not None and not isinstance(body[key], str):
            raise LimitPilotError(status_mod.tr(status_mod._lang(), "게이트웨이 입력값이 올바르지 않습니다",
                                      "Invalid gateway input"))
    with locks.state_lock():
        st = state.load_state()
        slot = state.slot_of(st, n)
        if not limits.is_gateway_slot(slot):
            raise LimitPilotError(status_mod.tr(status_mod._lang(), f"슬롯 {n}은 서드파티 게이트웨이가 아닙니다",
                                      f"Slot {n} is not a third-party gateway"))
        token = body.get("token") or creds.kc_read_slot(n)
        current_usage_config = slot.get("gatewayUsageConfig") or {}
    if not token:
        raise LimitPilotError(status_mod.tr(status_mod._lang(), "저장된 게이트웨이 토큰이 없습니다",
                                  "No saved gateway token"))
    base_url = body.get("baseUrl") or ""
    model = body.get("modelId") or ""
    auth_mode = body.get("authMode") or "bearer"
    alias = body.get("alias") or model
    usage_config = gateway.validate_gateway_usage_config(
        body.get("inputTokenLimit", current_usage_config.get("inputTokenLimit", 0)),
        body.get("outputTokenLimit", current_usage_config.get("outputTokenLimit", 0)),
        body.get("inputPricePerMillion", current_usage_config.get("inputPricePerMillion", 0)),
        body.get("outputPricePerMillion", current_usage_config.get("outputPricePerMillion", 0)),
        body.get("costLimit", current_usage_config.get("costLimit", 0)))
    gateway.probe_gateway_connection(base_url, model, token, auth_mode)
    with locks.state_lock():
        st = state.load_state()
        slot = state.slot_of(st, n)
        if not limits.is_gateway_slot(slot):
            raise LimitPilotError(status_mod.tr(status_mod._lang(), "검증 중 게이트웨이 슬롯이 변경되었습니다",
                                      "The gateway slot changed during verification"))
        _, label = gateway.update_gateway_slot(
            st, n, base_url, model, token, auth_mode, alias, usage_config)
    return {
        "slot": n, "label": label,
        "message": status_mod.tr(status_mod._lang(), f"슬롯 {n} 서드파티 게이트웨이 수정 완료",
                       f"Third-party gateway in slot {n} updated"),
    }


class Handler(BaseHTTPRequestHandler):
    def _local_host_ok(self) -> bool:
        # DNS rebinding 차단: 브라우저가 보낸 Host 가 우리 루프백 주소여야 한다
        port = self.server.server_address[1]
        host = self.headers.get("Host", "")
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _origin_ok(self) -> bool:
        # 다른 사이트에서 보낸 요청 차단: Origin 이 있으면 같은 주소여야 한다
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # 같은 출처 GET·CLI 클라이언트는 Origin 을 보내지 않을 수 있다
        port = self.server.server_address[1]
        return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def _token_ok(self, allow_query: bool = False) -> bool:
        expected = getattr(self.server, "api_token", None)
        supplied = self.headers.get(TOKEN_HEADER)
        if supplied is None and allow_query and "?" in self.path:
            # EventSource(SSE) 는 헤더를 못 붙인다 — 프레즌스만 쿼리로 받는다
            query = urllib.parse.parse_qs(self.path.split("?", 1)[1])
            supplied = (query.get("token") or [None])[0]
        return (isinstance(expected, str) and isinstance(supplied, str)
                and secrets.compare_digest(supplied, expected))

    def _local_post_ok(self) -> bool:
        # Host·Origin 검사 + 커스텀 헤더(교차 출처 단순 요청 차단)
        return (self._local_host_ok() and self._origin_ok()
                and self.headers.get("X-LimitPilot") == "1")

    def _read_json_body(self, limit: int = 4096) -> dict:
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > limit:
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_POST(self):
        if not self._local_post_ok():
            self.send_error(403)
            return
        # 실행별 토큰: 같은 PC 의 다른 프로세스·사용자가 헤더만 흉내 내서는 못 바꾼다.
        # /api/shutdown 은 런타임 기록(0600)의 nonce 로 따로 인증한다.
        if self.path != "/api/shutdown" and not self._token_ok():
            self.send_error(403)
            return
        try:
            if self.path == "/api/shutdown":
                body = self._read_json_body()
                expected = getattr(self.server, "shutdown_nonce", None)
                supplied = body.get("nonce")
                if (not isinstance(expected, str) or not isinstance(supplied, str)
                        or not secrets.compare_digest(supplied, expected)):
                    self.send_error(403)
                    return
                # OAuth worker가 state/Keychain을 다시 만들지 못하게 먼저 종료.
                _shutdown_requested.set()
                cancel_add(force=True)
                self._send(200, "application/json; charset=utf-8", b'{"ok": true}')
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path == "/api/settings":
                body = self._read_json_body()
                msg = apply_settings(body)
                self._send(200, "application/json; charset=utf-8",
                           json.dumps({"ok": True, "message": msg}, ensure_ascii=False).encode())
                return
            if self.path == "/api/add/current":
                # 온보딩 1단계: 지금 Claude Code 에 로그인된 계정을 등록(`limitpilot add`)
                with locks.state_lock():
                    st = state.load_state()
                    n, email, is_new = accounts.register_current_login(st)
                with status_mod._cache_lock:
                    status_mod._cache.pop(n, None)
                lang = state.get_settings(st)["language"]
                # CLI `add` 와 같은 문구 — 다시 등록이면 별칭·계정별 한도가 남았다고 알린다
                message = i18n.tl(lang, "slot_added" if is_new else "slot_readded", n=n, email=email)
                self._send(200, "application/json; charset=utf-8",
                           json.dumps({"ok": True, "slot": n, "message": message},
                                      ensure_ascii=False).encode())
                return
            if self.path == "/api/add/start":
                body = self._read_json_body()
                alias = body.get("alias")
                if alias is not None and not isinstance(alias, str):
                    raise LimitPilotError(status_mod.tr(status_mod._lang(), "별칭은 문자열이어야 합니다",
                                              "Alias must be a string"))
                info = start_add(alias)
                self._send(200, "application/json; charset=utf-8",
                           json.dumps({"ok": True, "url": info.get("url"),
                                       "sessionId": info.get("sessionId"),
                                       "message": status_mod.tr(status_mod._lang(), "브라우저에서 로그인 후 코드를 붙여넣으세요",
                                                     "Log in via the browser, then paste the code")},
                                      ensure_ascii=False).encode())
                return
            if self.path == "/api/gateway/add":
                body = self._read_json_body(limit=16384)
                result = add_gateway(body)
                self._send(200, "application/json; charset=utf-8",
                           json.dumps({"ok": True, **result}, ensure_ascii=False).encode())
                return
            gateway_edit = re.match(r"^/api/gateway/(\d+)/update$", self.path)
            if gateway_edit:
                body = self._read_json_body(limit=16384)
                result = edit_gateway(int(gateway_edit.group(1)), body)
                self._send(200, "application/json; charset=utf-8",
                           json.dumps({"ok": True, **result}, ensure_ascii=False).encode())
                return
            if self.path == "/api/add/code":
                body = self._read_json_body()
                msg = feed_add(body.get("code") or "", body.get("sessionId"))
                self._send(200, "application/json; charset=utf-8",
                           json.dumps({"ok": True, "message": msg}, ensure_ascii=False).encode())
                return
            if self.path == "/api/add/cancel":
                body = self._read_json_body()
                msg = cancel_add(body.get("sessionId"))
                self._send(200, "application/json; charset=utf-8",
                           json.dumps({"ok": True, "message": msg}, ensure_ascii=False).encode())
                return
            if self.path == "/api/refresh":
                result = refresh_all_usage()
                self._send(200, "application/json; charset=utf-8",
                           json.dumps({"ok": True, **result}, ensure_ascii=False).encode())
                return
            refresh_match = re.match(r"^/api/slot/(\d+)/refresh$", self.path)
            if refresh_match:
                result = refresh_usage(int(refresh_match.group(1)))
                self._send(200, "application/json; charset=utf-8",
                           json.dumps({"ok": True, **result}, ensure_ascii=False).encode())
                return
            limits_match = re.match(r"^/api/slot/(\d+)/limits$", self.path)
            if limits_match:
                body = self._read_json_body()
                msg = set_limits(int(limits_match.group(1)), body)
                self._send(200, "application/json; charset=utf-8",
                           json.dumps({"ok": True, "message": msg}, ensure_ascii=False).encode())
                return
            cap_match = re.match(r"^/api/slot/(\d+)/cap$", self.path)
            if cap_match:
                body = self._read_json_body()
                msg = set_weekly_cap(int(cap_match.group(1)), body.get("cap"))
                self._send(200, "application/json; charset=utf-8",
                           json.dumps({"ok": True, "message": msg}, ensure_ascii=False).encode())
                return
            alias_match = re.match(r"^/api/slot/(\d+)/alias$", self.path)
            if alias_match:
                body = self._read_json_body()
                msg = set_slot_alias(int(alias_match.group(1)), body.get("alias"))
                self._send(200, "application/json; charset=utf-8",
                           json.dumps({"ok": True, "message": msg}, ensure_ascii=False).encode())
                return
            m = re.match(r"^/api/slot/(\d+)/(activate|enable|disable|delete)$", self.path)
            if not m:
                self.send_error(404)
                return
            action = m.group(2)
            force = False
            if action == "activate":
                body = self._read_json_body()
                force = body.get("force", False)
                if not isinstance(force, bool):
                    raise LimitPilotError(status_mod.tr(status_mod._lang(), "force 값은 true 또는 false여야 합니다",
                                              "force must be true or false"))
            msg = mutate_slot(int(m.group(1)), action, force=force)
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"ok": True, "message": msg}, ensure_ascii=False).encode())
        except LimitPilotError as e:
            # 우리가 만든 검증 메시지만 — 토큰이 실릴 수 없는 경로
            self._send(400, "application/json; charset=utf-8",
                       json.dumps({"error": str(e)}, ensure_ascii=False).encode())
        except sandbox.SandboxViolation as e:
            # 연습 모드가 막은 실제 호출(게이트웨이 연결 확인 등) — 'internal' 대신 이유를 알린다.
            # 위반 문구는 실행 파일 이름·호스트:포트뿐이라 비밀이 실릴 수 없다.
            message = i18n.tl(status_mod._lang(), "sandbox_blocked", detail=e)
            self._send(400, "application/json; charset=utf-8",
                       json.dumps({"error": message}, ensure_ascii=False).encode())
        except Exception as e:
            print(i18n.t("http_error_log", where=f"POST {self.path}", err=type(e).__name__), file=sys.stderr)
            self._send(500, "application/json; charset=utf-8", b'{"error": "internal"}')

    def do_GET(self):
        global _presence_count
        if not self._local_host_ok() or not self._origin_ok():
            self.send_error(403)
            return
        route = self.path.split("?", 1)[0]
        if (route.startswith("/api/") and route != "/api/health"
                and not self._token_ok(allow_query=(route == "/api/presence"))):
            self.send_error(403)
            return
        if route in ("/", "/index.html"):
            try:
                body = read_html()
            except OSError:
                self.send_error(500, "dashboard.html missing")
                return
            # 페이지에 서버 빌드·실행별 토큰·연습 모드 표식을 새긴다
            token = getattr(self.server, "api_token", "") or ""
            body = (body.replace(BUILD_PLACEHOLDER, BUILD_ID.encode())
                    .replace(TOKEN_PLACEHOLDER, token.encode())
                    .replace(DEMO_PLACEHOLDER, b"1" if _demo_mode() else b""))
            self._send(200, "text/html; charset=utf-8", body)
        elif self.path == "/api/add/status":
            with _add_lock:
                st = dict(_add_status)
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(st, ensure_ascii=False).encode())
        elif self.path == "/api/status":
            try:
                # ETag: 상태 파일·라이브 캐시가 그대로면 상태를 다시 만들지 않고 304
                etag = status_mod.status_etag()
                if etag and self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    return
                body = json.dumps(status_mod.build_status(), ensure_ascii=False).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body, etag=etag)
            except Exception as e:
                # 예외 메시지는 응답에 싣지 않는다(토큰 포함 가능성) — 로컬 로그만
                print(i18n.t("http_error_log", where="/api/status", err=type(e).__name__), file=sys.stderr)
                self._send(500, "application/json; charset=utf-8",
                           b'{"error": "internal"}')
        elif self.path == "/api/health":
            with _presence_lock:
                clients = _presence_count
            body = json.dumps({
                "app": APP_ID, "version": APP_VERSION, "build": BUILD_ID,
                "adding": _add_session is not None,
                "instance": getattr(self.server, "instance_id", None),
                "ok": True, "clients": clients,
            }).encode()
            self._send(200, "application/json; charset=utf-8", body)
        elif self.path.split("?", 1)[0] == "/api/presence":
            # UI 창 프레즌스(SSE) — 창이 열려 있는 동안 연결이 유지되고 닫히면
            # 즉시 끊긴다. 재실행 시 '이미 열린 창' 판단에 사용(중복 창 방지).
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            current = f"build={BUILD_ID}" in query.split("&")
            if current:
                with _presence_lock:
                    _presence_count += 1
            try:
                while not _shutdown_requested.is_set():
                    # 이전 빌드 창은 새로고침 신호를 모르므로 세지 않고 연결만 유지
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    # 15초 — 창 닫힘은 다음 ping 에서 감지(스레드가 3초마다 깨지 않게)
                    _shutdown_requested.wait(15)
            except OSError:
                pass  # 창 닫힘 — 연결 종료
            finally:
                if current:
                    with _presence_lock:
                        _presence_count -= 1
        else:
            self.send_error(404)

    def _send(self, code: int, ctype: str, body: bytes, etag=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if etag:
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # 액세스 로그 소음 제거


def open_ui(url: str, browser: str, app_browser=None):
    """Open the page: a Chromium app window when possible, else the default browser.

    ``app_browser`` is the path of any Chromium-based browser executable / .app to use
    for the app window (``--app-browser``); ``auto`` tries Google Chrome.
    """
    if app_browser:
        if sys.platform == "darwin" and app_browser.endswith(".app") and os.path.isdir(app_browser):
            subprocess.Popen(["open", "-na", app_browser, "--args", f"--app={url}"])
            return
        if os.path.isfile(app_browser):
            subprocess.Popen([app_browser, f"--app={url}"])
            return
    if sys.platform == "win32":
        if browser in ("auto", "chrome"):
            for exe in (
                os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            ):
                if os.path.isfile(exe):
                    subprocess.Popen([exe, f"--app={url}"])
                    return
        os.startfile(url)  # 기본 브라우저
        return
    if browser in ("auto", "chrome") and os.path.isdir("/Applications/Google Chrome.app"):
        subprocess.Popen(["open", "-na", "Google Chrome", "--args", f"--app={url}"])
        return
    subprocess.Popen(["open", url])


def _open_or_tell(url: str, browser: str, app_browser=None) -> None:
    """Open the page, or print the address when a browser cannot be started.

    Practice mode starts no other programs (the sandbox would block ``open``), so it says
    so and prints the address instead of failing. A browser that fails to start never
    stops the dashboard itself.
    """
    if _demo_mode():
        print(_t("dash_open_practice", url=url))
        return
    try:
        open_ui(url, browser, app_browser)
    except OSError as e:
        print(_t("dash_open_failed", url=url, err=e.strerror or type(e).__name__), file=sys.stderr)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def get_health(port: int):
    """실행 중인 대시보드의 /api/health 응답(dict) — 아니면 None."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as resp:
            data = json.loads(resp.read().decode())
        if data.get("app") == APP_ID and data.get("ok") is True:
            return data
    except Exception:
        pass
    return None


def dashboard_is_running(port: int) -> bool:
    return get_health(port) is not None


def purge_in_progress() -> bool:
    return os.path.exists(os.path.join(paths.STATE_DIR, ".purging"))


WINDOW_TITLE = "LimitPilot"

# Chromium 계열(Chrome/Edge/Arc/Brave) — 각 window 의 title 로 앱 창을 찾는다.
# 찾으면 그 창을 맨 앞(index 1)으로 올리고 앱을 활성화한다.
_FOCUS_CHROMIUM = '''
    if application "%(app)s" is running then
        tell application "%(app)s"
            repeat with w in windows
                if title of w contains "%(title)s" then
                    set index of w to 1
                    activate
                    return "ok"
                end if
            end repeat
        end tell
    end if
    return "no"
'''

# Safari — 탭 이름으로 찾아 해당 탭을 현재 탭으로 만들고 창을 앞으로.
_FOCUS_SAFARI = '''
    if application "Safari" is running then
        tell application "Safari"
            repeat with w in windows
                repeat with t in tabs of w
                    if name of t contains "%(title)s" then
                        set current tab of w to t
                        set index of w to 1
                        activate
                        return "ok"
                    end if
                end repeat
            end tell
        end tell
    end if
    return "no"
'''


def focus_ui(browser: str) -> bool:
    """이미 열린 대시보드 창을 찾아 맨 앞으로 가져온다(최선 노력, macOS).
    성공하면 True. 중복 창을 만들지 않는 것이 목적이라, 창을 못 찾으면 False를
    돌려 호출자가 새 창을 열지 결정하게 한다. 실패해도 예외는 삼킨다.

    open_ui 가 Chrome 앱 모드를 우선 쓰므로 그것을 먼저 확인하고, 다른 Chromium
    브라우저와 Safari 까지 훑어 어디에 떠 있어도 앞으로 올린다."""
    if sys.platform != "darwin":
        return False
    chromium = ["Google Chrome", "Microsoft Edge", "Brave Browser", "Arc"]
    seen = set()
    for app in chromium:
        if app in seen:
            continue
        seen.add(app)
        try:
            r = subprocess.run(
                ["osascript", "-e", _FOCUS_CHROMIUM % {"app": app, "title": WINDOW_TITLE}],
                capture_output=True, text=True, timeout=5)
            if r.stdout.strip() == "ok":
                return True
        except Exception:
            pass
    try:
        r = subprocess.run(["osascript", "-e", _FOCUS_SAFARI % {"title": WINDOW_TITLE}],
                           capture_output=True, text=True, timeout=5)
        if r.stdout.strip() == "ok":
            return True
    except Exception:
        pass
    return False


def _reuse_running(url: str, args, health: dict) -> None:
    """이미 실행 중인 대시보드를 재사용한다 — 새 창을 만들지 않고 기존 창을 앞으로.

    창이 열려 있으면(프레즌스 clients>0) focus_ui로 맨 앞에 올리고, focus에 실패해도
    (프레즌스가 창을 알고 있으므로) 중복 창을 열지 않는다. 창이 없으면(clients==0)
    혹시 남은 창을 먼저 raise 시도하고, 그래도 없을 때만 새 창을 연다."""
    if args.no_open:
        print(_t("dash_already_running", url=url))
        return
    if _demo_mode():
        _open_or_tell(url, args.browser)   # 연습 모드: 창 찾기(osascript)·열기 대신 주소 안내
        return
    if health.get("clients", 0) > 0:
        focus_ui(args.browser)
        print(_t("dash_focus_existing", url=url))
        return
    if focus_ui(args.browser):
        print(_t("dash_focus_existing", url=url))
    else:
        _open_or_tell(url, args.browser, getattr(args, "app_browser", None))
        print(_t("dash_reopen", url=url))


def _listening_pids(port: int):
    """127.0.0.1:port를 LISTEN 중인 PID 집합(최선 노력). 실패하면 빈 집합."""
    pids = set()
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
                timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            for line in (out or "").splitlines():
                cols = line.split()
                # 상태 열(LISTENING)은 로캘에 따라 번역되므로 원격 주소(0.0.0.0:0)로도 판정
                listening = cols[3:4] and (cols[3].upper() == "LISTENING"
                                           or cols[2] in ("0.0.0.0:0", "[::]:0"))
                if (len(cols) >= 5 and listening
                        and cols[1].endswith(f":{port}") and cols[4].isdigit()):
                    pids.add(int(cols[4]))
        else:
            out = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True, text=True, timeout=5).stdout
            pids = {int(x) for x in (out or "").split() if x.isdigit()}
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()
    return pids


def _port_settled(port: int) -> bool:
    """포트가 실제로 비었거나(연결 거부) 이미 현재 빌드가 차지했으면 True.
    health 무응답만으로는 판정하지 않는다 — 종료 중인 서버는 응답을 멈춘 뒤에야
    소켓을 닫으므로 그 사이에 bind하면 EADDRINUSE가 난다. 현재 빌드라면 동시에 실행된
    다른 실행기가 먼저 교체를 끝낸 것이라 main의 bind 실패 경로가 재사용으로 처리한다."""
    if not port_in_use(port):
        return True
    health = get_health(port)
    return bool(health) and health.get("build") == BUILD_ID


def _wait_port_released(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_settled(port):
            return True
        time.sleep(0.1)
    return _port_settled(port)


def stop_stale_dashboard(port: int, health: dict) -> bool:
    """Stop a dashboard of another build on ``port`` → True when the port is free.

    Only a dashboard we can prove is ours is stopped: its runtime record must name this
    port and the instance id its /api/health reports. It is asked to exit with the
    record's nonce; if it does not, the *recorded* pid is terminated only when that pid
    is the one listening on the port. Unknown listeners are never killed.
    """
    instance = health.get("instance")
    try:
        records = runtime._dashboard_runtime_records()
    except LimitPilotError:
        records = []
    verified_pid = None
    for _path, rec_pid, rec_port, nonce, rec_instance in records:
        if rec_port != port or not instance or rec_instance != instance:
            continue
        verified_pid = rec_pid
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/shutdown",
            data=json.dumps({"nonce": nonce}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-LimitPilot": "1",
                     "Host": f"127.0.0.1:{port}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                response.read(256)
        except Exception:
            break  # 정상 종료 실패 — 아래의 확인된 pid 종료로
        if _wait_port_released(port, STALE_STOP_TIMEOUT_S):
            return True
        break
    if _port_settled(port):
        return True  # 그 사이 다른 실행기가 교체를 끝냈다 — 새 서버를 죽이지 않는다
    if verified_pid is None or verified_pid == os.getpid():
        return False  # 기록으로 확인되지 않은 프로세스는 종료하지 않는다
    if verified_pid not in _listening_pids(port):
        return False  # 기록의 pid 가 지금 그 포트의 주인이 아니다(pid 재사용 등)
    try:
        os.kill(verified_pid, signal.SIGTERM)
    except OSError:
        return False
    return _wait_port_released(port, STALE_STOP_TIMEOUT_S)


def _open_after_replace(url: str, browser: str) -> None:
    """교체 직후 — 기존 창(현재 빌드 JS)이 새로고침해 다시 붙으면 그 창을 앞으로,
    REOPEN_WAIT_S 안에 안 붙으면(창이 없거나 이전 빌드 창뿐) 새 창을 연다."""
    deadline = time.monotonic() + REOPEN_WAIT_S
    while time.monotonic() < deadline:
        with _presence_lock:
            if _presence_count > 0:
                focus_ui(browser)
                return
        time.sleep(0.2)
    _open_or_tell(url, browser)


def _interrupt_server(_signum, _frame):
    raise KeyboardInterrupt


def _demo_mode() -> bool:
    return os.environ.get("LIMITPILOT_DEMO") == "1"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="limitpilot dashboard",
                                description="LimitPilot local dashboard (127.0.0.1)")
    p.add_argument("--port", type=int, default=paths.DASHBOARD_DEFAULT_PORT)
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    p.add_argument("--browser", choices=["auto", "chrome", "default"], default="auto")
    p.add_argument("--app-browser", help="path of a Chromium-based browser for the app window")
    p.add_argument("--add", action="store_true", help="open with the add-account panel")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    url = f"http://127.0.0.1:{args.port}/"
    page_url = url + ("#add" if args.add else "")  # 위젯의 "브라우저로 계정 추가…"

    if purge_in_progress():
        print(_t("dash_purging"), file=sys.stderr)
        return 1

    # 이 대시보드와 같은 코드로 데몬 사본을 맞춘다 — 대시보드만 새 빌드이고 데몬은
    # 예전 코드인 불일치 방지(데몬 미설치·동일하면 no-op, 실패는 로그만)
    if not _demo_mode():
        deploy.maybe_refresh_daemon_copy()

    replaced = False
    if port_in_use(args.port):
        health = get_health(args.port)
        if not health:
            print(_t("dash_port_busy", port=args.port), file=sys.stderr)
            return 1
        if health.get("build") == BUILD_ID:
            # 같은 빌드의 대시보드가 이미 실행 중 — 중복 창을 만들지 않고 기존 창을 앞으로.
            _reuse_running(page_url, args, health)
            return 0
        running_v, mine_v = deploy.version_tuple(health.get("version")), deploy.version_tuple(APP_VERSION)
        if running_v and mine_v and running_v > mine_v:
            # 더 새 버전이 떠 있다 — 예전 앱을 실행했다고 새 서버를 내리지 않는다
            print(_t("dash_newer_running", version=health.get("version")))
            _reuse_running(page_url, args, health)
            return 0
        if health.get("adding"):
            # 진행 중인 브라우저 계정 추가를 끊지 않는다 — 끝난 뒤 다시 열면 교체된다
            print(_t("dash_add_in_progress"), file=sys.stderr)
            _reuse_running(page_url, args, health)
            return 0
        # 다른 빌드(업데이트 전 코드)가 떠 있다 — 재사용하면 새 기능이 안 보이므로 교체
        if not stop_stale_dashboard(args.port, health):
            print(_t("dash_old_not_stopped"), file=sys.stderr)
            _reuse_running(page_url, args, health)
            return 0
        print(_t("dash_replacing", build=BUILD_ID))
        replaced = True

    server, bind_error = None, None
    deadline = time.monotonic() + (STALE_STOP_TIMEOUT_S if replaced else 0)
    while server is None:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        except OSError as e:
            bind_error = e
            # 교체 직후엔 이전 서버가 소켓을 닫는 중일 수 있다 — 잠깐 재시도
            if time.monotonic() >= deadline or get_health(args.port):
                break
            time.sleep(0.2)
    if server is None:
        e = bind_error
        # 포트 확인과 bind 사이에 경합이 있었더라도 앱 식별 후에만 재사용한다.
        health = get_health(args.port)
        if health:
            _reuse_running(page_url, args, health)
            return 0
        print(_t("dash_cannot_start", err=e), file=sys.stderr)
        return 1
    if purge_in_progress():
        server.server_close()
        print(_t("dash_purging"), file=sys.stderr)
        return 1
    runtime_path = os.path.join(
        paths.STATE_DIR, f"{paths.DASHBOARD_RUNTIME_PREFIX}{os.getpid()}.json",
    )
    _shutdown_requested.clear()
    server.shutdown_nonce = secrets.token_urlsafe(32)
    server.instance_id = secrets.token_urlsafe(18)
    server.api_token = secrets.token_urlsafe(32)  # 실행마다 새 API 토큰(페이지에만 심는다)
    status_mod._BACKGROUND_SCAN = True  # 게이트웨이 집계는 백그라운드 스레드로
    try:
        os.makedirs(paths.STATE_DIR, mode=0o700, exist_ok=True)
        filestore.atomic_write_json(runtime_path, {
            "app": APP_ID,
            "version": APP_VERSION,
            "pid": os.getpid(),
            "port": args.port,
            "nonce": server.shutdown_nonce,
            "instance": server.instance_id,
        })
    except OSError as e:
        server.server_close()
        print(_t("dash_runtime_save_failed", err=e), file=sys.stderr)
        return 1
    if purge_in_progress():
        server.server_close()
        try:
            os.remove(runtime_path)
        except FileNotFoundError:
            pass
        print(_t("dash_purging"), file=sys.stderr)
        return 1
    print(_t("dash_started", url=url) + (_t("dash_demo_suffix") if _demo_mode() else ""))
    try:
        # 창 열기도 try 안에서 — 어떤 실패에도 아래 finally 가 소켓을 닫고 실행 기록을 지운다
        if not args.no_open:
            if replaced:
                threading.Thread(target=_open_after_replace, args=(page_url, args.browser),
                                 daemon=True).start()
            else:
                _open_or_tell(page_url, args.browser, getattr(args, "app_browser", None))
        signal.signal(signal.SIGTERM, _interrupt_server)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cancel_add(force=True)
        server.server_close()
        try:
            os.remove(runtime_path)
        except FileNotFoundError:
            pass
    return 0
