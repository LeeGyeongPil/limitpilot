"""Shared fixtures: temp homes, the fake credential store, a fake usage source."""

import contextlib
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from unittest import mock

from limitpilot import paths
from limitpilot import usage_api
from limitpilot.store import fake

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")


def iso(offset_s: float) -> str:
    return datetime.fromtimestamp(time.time() + offset_s, timezone.utc).isoformat()


def usage(session=0.0, weekly=0.0, session_reset_s=3600, weekly_reset_s=6 * 86400):
    return usage_api.normalize_usage({"limits": [
        {"kind": "session", "percent": session, "resets_at": iso(session_reset_s)},
        {"kind": "weekly_all", "percent": weekly, "resets_at": iso(weekly_reset_s)},
    ]})


def blob(access="fake-access", refresh="fake-refresh", valid_s=3600 * 8) -> str:
    return json.dumps({"claudeAiOauth": {
        "accessToken": access, "refreshToken": refresh,
        "expiresAt": int((time.time() + valid_s) * 1000),
        "scopes": ["user:profile", "user:inference", "user:sessions:claude_code",
                   "user:mcp_servers", "user:file_upload"],
    }})


def identity(n: int) -> dict:
    return {"accountUuid": f"acct-{n}", "organizationUuid": f"org-{n}",
            "emailAddress": f"user{n}@example.com", "organizationName": "Example Org"}


@contextlib.contextmanager
def temp_home():
    """Point the state folder and the Claude Code home at a fresh temp folder."""
    root = tempfile.mkdtemp(prefix="limitpilot-test-")
    state_dir = os.path.join(root, "state")
    claude_home = os.path.join(root, "home")
    os.makedirs(os.path.join(claude_home, ".claude"))
    patches = [
        mock.patch.object(paths, "STATE_DIR", state_dir),
        mock.patch.object(paths, "HOME", claude_home),
        mock.patch.object(paths, "CLAUDE_JSON", os.path.join(claude_home, ".claude.json")),
        mock.patch.object(paths, "CLAUDE_SETTINGS_FILE",
                          os.path.join(claude_home, ".claude", "settings.json")),
        mock.patch.object(paths, "CC_CREDS_FILE",
                          os.path.join(claude_home, ".claude", ".credentials.json")),
        mock.patch.object(paths, "CRED_LOCK_DIR", claude_home + "/.claude.lock"),
        mock.patch.object(paths, "CONF_LOCK_DIR", os.path.join(claude_home, ".claude.json.lock")),
        mock.patch.object(paths, "LAUNCHD_PLIST",
                          os.path.join(claude_home, "Library", "LaunchAgents", "lp.plist")),
    ]
    for p in patches:
        p.start()
    try:
        yield root
    finally:
        for p in reversed(patches):
            p.stop()
        shutil.rmtree(root, ignore_errors=True)


@contextlib.contextmanager
def fake_store(root: str):
    fake.activate(os.path.join(root, "fake-store.json"))
    try:
        yield fake
    finally:
        fake.deactivate()


class FakeUsage(usage_api.UsageSource):
    """Usage per access token, with an optional delay (to see what happens meanwhile)."""

    def __init__(self, table: dict, delay: float = 0.0, during=None):
        self.table = table
        self.delay = delay
        self.during = during
        self.calls = 0

    def fetch(self, access_token):
        self.calls += 1
        if self.during:
            self.during()
        if self.delay:
            time.sleep(self.delay)
        u = self.table.get(access_token)
        return (u, None, None) if u else (None, "http-401", None)

    def fetch_raw(self, access_token):
        return None, "unused"

    def refresh(self, blob_text):
        return None, "invalid_grant"


@contextlib.contextmanager
def usage_source(source):
    usage_api.set_source(source)
    try:
        yield source
    finally:
        usage_api.set_source(None)
