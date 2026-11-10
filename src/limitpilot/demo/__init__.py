"""Practice mode (``--demo``): three fake accounts in a throw-away folder.

What is fake — only the I/O boundary:

* credential store → ``store/fake.py`` (a JSON file in the demo folder),
* Claude Code files → a fake ``.claude.json`` in the demo folder,
* usage endpoint and token refresh → ``ScenarioSource`` reading ``scenario.json``,
* notifications → printed, launchd / Task Scheduler / network → blocked by the sandbox.

What is real — everything else: collection budget, ``policy.decide``, ``switch``
(write, read back, roll back), state saving, the CLI, the dashboard.

The folder is ``<system temp>/limitpilot-demo`` (or ``LIMITPILOT_DEMO_ROOT``).
"""

import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from importlib import resources

from limitpilot.constants import LOGIN_REQUIRED_SCOPES
from limitpilot import usage_api

DEMO_VERSION = 1


def root() -> str:
    return os.path.abspath(os.environ.get("LIMITPILOT_DEMO_ROOT")
                           or os.path.join(tempfile.gettempdir(), "limitpilot-demo"))


def load_scenario() -> dict:
    data = resources.files("limitpilot.demo").joinpath("scenario.json").read_text("utf-8")
    return json.loads(data)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _blob(access: str, refresh: str, expires_ms: float) -> str:
    """Credential JSON in Claude Code's shape, with obviously fake values."""
    return json.dumps({"claudeAiOauth": {
        "accessToken": access, "refreshToken": refresh, "expiresAt": int(expires_ms),
        "scopes": sorted(LOGIN_REQUIRED_SCOPES), "subscriptionType": "pro",
    }})


def _identity(acc: dict) -> dict:
    return {"accountUuid": acc["accountUuid"], "organizationUuid": acc["orgUuid"],
            "emailAddress": acc["email"], "organizationName": acc["org"],
            "displayName": acc["alias"]}


class ScenarioSource(usage_api.UsageSource):
    """Usage and token refresh from ``scenario.json`` instead of the network."""

    def __init__(self, scenario: dict, start: float):
        self.scenario = scenario
        self.start = start
        self.refreshes = 0

    def _account(self, token: str):
        for acc in self.scenario["accounts"]:
            if token == acc["access"] or token.startswith(acc["access"] + "-r"):
                return acc
            if token == acc["refresh"] or token.startswith(acc["refresh"] + "-r"):
                return acc
        return None

    def _tick(self) -> int:
        step = self.scenario["tickMinutes"] * 60
        return max(0, int((time.time() - self.start) // step))

    def fetch_raw(self, access_token: str):
        acc = self._account(access_token)
        if acc is None:
            return None, "http-401"
        i = min(self._tick(), len(acc["session"]) - 1)
        return {"limits": [
            {"kind": "session", "percent": acc["session"][i],
             "resets_at": _iso(self.start + acc["sessionResetMinutes"] * 60)},
            {"kind": "weekly_all", "percent": acc["weekly"][i],
             "resets_at": _iso(self.start + acc["weeklyResetHours"] * 3600)},
        ]}, None

    def fetch(self, access_token: str):
        raw, err = self.fetch_raw(access_token)
        return (usage_api.normalize_usage(raw) if raw else None), err, None

    def keepalive(self, access_token: str):
        """Stand-in for the one-token keep-alive message: nothing is sent anywhere.

        A token the scenario knows "answers" OK; an unknown one gets 401, like the real API.
        """
        if self._account(access_token) is None:
            return False, "http-401", None
        return True, None, None

    def refresh(self, blob: str):
        try:
            oauth = json.loads(blob)["claudeAiOauth"]
        except (ValueError, KeyError, TypeError):
            return None, "no_refresh_token"
        acc = self._account(oauth.get("refreshToken") or "")
        if acc is None:
            return None, "invalid_grant"
        if acc.get("refreshFails"):
            return None, acc["refreshFails"]   # 예: 비밀번호를 바꿔 토큰이 죽은 계정
        self.refreshes += 1
        n = self.refreshes
        # 서버가 refresh token 을 회전시킨다 — 이 새 값만 유효하다
        return _blob(f"{acc['access']}-r{n}", f"{acc['refresh']}-r{n}",
                     (time.time() + 8 * 3600) * 1000), None


def build_home(base: str, fresh: bool = False) -> float:
    """Create the fake homes and return the scenario start time (epoch seconds)."""
    from limitpilot import paths, state
    from limitpilot.store import fake, filestore

    scenario = load_scenario()
    start = time.time()
    # 지우기 전 안전 확인: 우리가 만든 연습 폴더(표식 파일) 또는 이름이 limitpilot-demo* 일 때만
    if os.path.isdir(base):
        ours = (os.path.exists(os.path.join(base, "demo.json"))
                or os.path.basename(base).startswith("limitpilot-demo"))
        if not ours:
            raise RuntimeError(f"refusing to reset a folder that is not a demo folder: {base}")
        shutil.rmtree(base, ignore_errors=True)
    os.makedirs(os.path.join(base, "claude-home", ".claude"), mode=0o700, exist_ok=True)
    os.makedirs(os.path.join(base, "state"), mode=0o700, exist_ok=True)
    paths.configure()
    fake.activate(os.path.join(base, "fake-store.json"))
    accounts = scenario["accounts"]
    # 가짜 '활성' 자격증명 = 계정 1 (Claude Code 가 지금 로그인해 있는 계정)
    first = accounts[0]
    filestore.atomic_write_json(paths.CLAUDE_JSON, {"oauthAccount": _identity(first)})
    for acc in accounts:
        expires = (start + acc["tokenValidHours"] * 3600) * 1000
        fake.write(paths.BACKUP_SERVICE, paths.backup_account(acc["slot"]),
                   _blob(acc["access"], acc["refresh"], expires))
        if acc is first:
            fake.write(paths.ACTIVE_SERVICE, paths.KC_USER,
                       _blob(acc["access"], acc["refresh"], expires))
    st = state.default_state()
    st["settings"] = {"threshold": scenario["threshold"], "autoSwitch": True}
    if not fresh:
        for acc in accounts:
            st["slots"][str(acc["slot"])] = {
                "email": acc["email"], "alias": acc["alias"], "orgName": acc["org"],
                "addedAt": _iso(start - 86400), "oauthAccount": _identity(acc),
                "enabled": True, "kind": "oauth", "quarantine": None,
            }
        st["activeSlot"] = first["slot"]
        st["manualChoiceAt"] = start   # 방금 직접 고른 계정 — 리셋 임박 선제 이동 유예
    else:
        for acc in accounts:   # --fresh: 등록 전 상태(온보딩 화면용) — 저장소의 백업은 비운다
            fake.delete(paths.BACKUP_SERVICE, paths.backup_account(acc["slot"]))
    state.save_state(st)
    _build_previous_tool_home(accounts[:2], start)
    filestore.atomic_write_json(os.path.join(base, "demo.json"),
                                {"version": DEMO_VERSION, "start": start, "fresh": fresh})
    return start


def _build_previous_tool_home(accounts, start: float) -> None:
    """A fake claude-account-pilot folder so `--demo migrate --dry-run` has something to show."""
    from limitpilot import legacy, paths
    from limitpilot.store import fake, filestore

    old = legacy.state_dir()
    os.makedirs(old, mode=0o700, exist_ok=True)
    slots = {str(acc["slot"]): {"email": acc["email"], "alias": acc["alias"], "enabled": True,
                                "kind": "oauth", "oauthAccount": _identity(acc)}
             for acc in accounts}
    filestore.atomic_write_json(os.path.join(old, "state.json"), {
        "schemaVersion": 1, "activeSlot": accounts[0]["slot"], "slots": slots,
        "settings": {"threshold": 90}, "lastNotify": {}})
    for acc in accounts:
        fake.write(legacy.KEYCHAIN_SERVICE, paths.backup_account(acc["slot"]),
                   _blob(acc["access"], acc["refresh"], (start + 3600) * 1000))


def activate(fresh: bool = False, rebuild: bool = False) -> float:
    """Switch this process into practice mode and return the scenario start time."""
    from limitpilot import paths, sandbox
    from limitpilot.store import fake

    base = root()
    os.environ["LIMITPILOT_SANDBOX"] = "1"
    os.environ["LIMITPILOT_DEMO"] = "1"
    os.environ["LIMITPILOT_HOME"] = os.path.join(base, "state")
    os.environ["LIMITPILOT_CLAUDE_HOME"] = os.path.join(base, "claude-home")
    sandbox.install()
    paths.configure()
    marker = os.path.join(base, "demo.json")
    start = None
    if not (fresh or rebuild):
        try:
            with open(marker, encoding="utf-8") as handle:
                info = json.load(handle)
            if info.get("version") == DEMO_VERSION:
                start = float(info["start"])
        except (OSError, ValueError, KeyError):
            start = None
    if start is None:
        start = build_home(base, fresh=fresh)
    fake.activate(os.path.join(base, "fake-store.json"))
    usage_api.set_source(ScenarioSource(load_scenario(), start))
    return start
