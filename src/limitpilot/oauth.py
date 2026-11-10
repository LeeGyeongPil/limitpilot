"""OAuth credential helpers: parse Claude Code's credential JSON, expiry checks, refresh.

Only inactive backup slots are ever refreshed — the active credential belongs to Claude
Code, which renews it itself.
"""

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from limitpilot.constants import (CLIENT_ID, EXPIRY_BUFFER_MS, TOKEN_URL, USER_AGENT)


# -------------------------------------------------------------------- oauth


def parse_oauth(blob: str):
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return oauth if isinstance(oauth, dict) else None


def is_expired(oauth: dict, buffer_ms: int = EXPIRY_BUFFER_MS) -> bool:
    expires_at = oauth.get("expiresAt")
    if not isinstance(expires_at, (int, float)):
        # 만료시각 불명은 미만료로 취급(레퍼런스 동일) — 유효할 수 있는 토큰의
        # 불필요한 회전(리프레시)을 유발하지 않는다
        return False
    now_ms = time.time() * 1000
    return now_ms + buffer_ms >= expires_at


def refresh_fingerprint(blob: str):
    oauth = parse_oauth(blob)
    rt = (oauth or {}).get("refreshToken")
    if not rt:
        return None
    return "sha256:" + hashlib.sha256(rt.encode()).hexdigest()[:16]


def try_refresh(blob: str):
    """리프레시 시도 → (새 blob | None, outcome). outcome:
    None=성공 / "invalid_grant"·"no_refresh_token"=영구 / "transient"=일시."""
    from limitpilot import usage_api

    if usage_api.source() is not None:
        return usage_api.source().refresh(blob)   # 연습 모드·테스트 소스
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None, "no_refresh_token"
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("refreshToken"):
        return None, "no_refresh_token"
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": oauth["refreshToken"],
        "client_id": CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_data = json.loads(resp.read().decode())
        now_ms = int(time.time() * 1000)
        oauth["accessToken"] = resp_data["access_token"]
        oauth["expiresAt"] = now_ms + resp_data["expires_in"] * 1000
        if resp_data.get("refresh_token"):
            oauth["refreshToken"] = resp_data["refresh_token"]
        if resp_data.get("scope"):
            oauth["scopes"] = resp_data["scope"].split()
        data["claudeAiOauth"] = oauth
        return json.dumps(data), None
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode(errors="replace")
        except OSError:
            err_body = ""
        # 4xx + 명시 마커일 때만 영구 판정 — 오격리(2계정 중 50% 상실)가 재시도 1회보다 훨씬 비쌈
        if e.code in (400, 401, 403) and (
            "invalid_grant" in err_body or "invalid_client" in err_body
        ):
            return None, "invalid_grant"
        return None, "transient"
    except Exception:
        return None, "transient"
