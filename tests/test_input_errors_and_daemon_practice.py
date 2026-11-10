"""QA regressions: friendly input errors, practice-mode commands, en output, re-registration.

Round 1: input errors, practice-mode daemon commands. Round 2: registering an account again
keeps its alias and per-account limits, ``gateway-add`` explains a missing env file, and
``login`` / ``keepalive`` say plainly that practice mode is why nothing real happens.
"""

import contextlib
import http.client
import io
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from limitpilot import accounts
from limitpilot import cli
from limitpilot import collector
from limitpilot import demo
from limitpilot import gateway
from limitpilot import i18n
from limitpilot import paths
from limitpilot import sandbox
from limitpilot import state
from limitpilot import usage_api
from limitpilot.dashboard import server
from limitpilot.login import session
from limitpilot.policy import limits
from limitpilot.store import creds
from tests.support import SRC, blob, fake_store, identity, temp_home

HANGUL = re.compile("[가-힣]")


def run_cli(*argv, lang="en"):
    """Run the CLI in-process (English unless told otherwise); return (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    saved_override, saved_env = i18n._override, os.environ.get("LIMITPILOT_LANG")
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--lang", lang, *argv])
            except SystemExit as exc:  # argparse errors
                rc = exc.code
    finally:  # --lang 이 다른 테스트로 새지 않게 되돌린다
        i18n._override = saved_override
        if saved_env is None:
            os.environ.pop("LIMITPILOT_LANG", None)
        else:
            os.environ["LIMITPILOT_LANG"] = saved_env
    return rc, out.getvalue(), err.getvalue()


class InputErrorTests(unittest.TestCase):
    def test_non_numeric_threshold_is_a_friendly_error(self):
        with temp_home():
            rc, out, err = run_cli("config", "threshold", "abc")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("Traceback", out + err)
        self.assertIn("must be a number", out + err)

    def test_out_of_range_threshold_is_english(self):
        with temp_home():
            rc, out, err = run_cli("config", "threshold", "200")
        self.assertNotEqual(rc, 0)
        self.assertIn("between 50% and 99%", out + err)
        self.assertIsNone(HANGUL.search(out + err))

    def test_bad_ports_are_rejected_before_any_socket_is_used(self):
        for bad in ("99999", "0", "-1", "abc"):
            with temp_home():
                rc, out, err = run_cli("dashboard", "--port", bad, "--no-open")
            self.assertEqual(rc, 2, bad)
            self.assertIn("1 to 65535", err)
            self.assertNotIn("Traceback", err)

    def test_unexpected_exception_becomes_one_line_unless_debug(self):
        def boom(_args):
            raise OverflowError("boom")
        with temp_home():
            from unittest import mock
            with mock.patch.object(cli, "cmd_list", boom):
                rc, _out, err = run_cli("list")
        self.assertEqual(rc, 1)
        self.assertIn("limitpilot doctor", err)
        self.assertNotIn("Traceback", err)

    def test_representative_errors_have_no_korean_in_english_mode(self):
        cases = [("config", "auto-switch", "maybe"), ("remove", "99"), ("cap", "1", "500"),
                 ("limits", "1", "--session", "300"), ("switch", "99")]
        for argv in cases:
            with temp_home():
                rc, out, err = run_cli(*argv)
            self.assertNotEqual(rc, 0, argv)
            self.assertIsNone(HANGUL.search(out + err), (argv, out + err))


class PracticeDaemonTests(unittest.TestCase):
    """In practice (--demo) mode no launchctl/schtasks call and no stop marker."""

    def setUp(self):
        from unittest import mock
        env = mock.patch.dict(os.environ, {"LIMITPILOT_DEMO": "1"})
        env.start()
        self.addCleanup(env.stop)

    def _marker(self):
        return os.path.join(paths.STATE_DIR, ".daemon-stopped")

    def test_install_and_uninstall_simulate_and_leave_no_marker(self):
        for argv in (("install-daemon",), ("install-daemon", "--enable-auto"),
                     ("uninstall-daemon",)):
            with temp_home():
                rc, out, err = run_cli(*argv)
                self.assertEqual(rc, 0, (argv, err))
                self.assertIn("Practice mode", out)
                self.assertNotIn("Traceback", err)
                self.assertFalse(os.path.exists(self._marker()), argv)

    def test_enable_auto_turns_on_auto_switch_in_practice_state_only(self):
        with temp_home():
            run_cli("install-daemon", "--enable-auto")
            self.assertTrue(state.get_settings(state.load_state())["autoSwitch"])

    def test_auto_explains_why_it_ends_when_the_stop_marker_exists(self):
        with temp_home():
            os.makedirs(paths.STATE_DIR, exist_ok=True)
            open(self._marker(), "w").close()
            for argv in (("auto",), ("auto", "--once")):
                rc, out, err = run_cli(*argv)
                self.assertIn("watcher is stopped", err, argv)
                self.assertIn("install-daemon", err)
                self.assertNotIn("Watching started", out)

    def test_sandbox_violation_becomes_one_friendly_line(self):
        from unittest import mock
        from limitpilot import sandbox
        def blocked(_args):
            raise sandbox.SandboxViolation("sandbox blocked launchctl: launchctl")
        with temp_home(), mock.patch.object(cli, "cmd_list", blocked):
            rc, _out, err = run_cli("list")
        self.assertEqual(rc, 1)
        self.assertIn("Blocked in practice mode", err)
        self.assertNotIn("Traceback", err)

    def test_daemon_ticks_stay_quiet_when_stopped(self):
        with temp_home():
            os.makedirs(paths.STATE_DIR, exist_ok=True)
            open(self._marker(), "w").close()
            from unittest import mock
            with mock.patch.dict(os.environ, {"LIMITPILOT_DAEMON": "1"}):
                rc, out, err = run_cli("auto", "--once")
        self.assertEqual(err, "")


# ---------------------------------------------------------------- round 2 (QA Q-1)

# 사용자가 계정마다 정한 값 — 다시 등록해도 남아야 한다
USER_SETTINGS = {"alias": "work", "sessionThreshold": 80.0, "weeklyThreshold": 75.0,
                 "weeklyCap": 70.0}


def _registered_slot(n, **extra):
    slot = {"email": f"user{n}@example.com", "orgName": "Example Org",
            "addedAt": "2026-01-02T03:04:05+00:00", "oauthAccount": identity(n),
            "enabled": True, "kind": "oauth", "quarantine": None}
    slot.update(extra)
    return slot


class ReRegistrationKeepsSettingsTests(unittest.TestCase):
    """Registering an already registered account (add / login / dashboard) refreshes the
    credential but keeps the alias, the per-account thresholds and the weekly maximum."""

    def _world(self, store, slot_extra):
        """Claude Code is signed in with account 1, which already has slot 1."""
        with open(paths.CLAUDE_JSON, "w", encoding="utf-8") as handle:
            json.dump({"oauthAccount": identity(1)}, handle)
        store.write(paths.ACTIVE_SERVICE, paths.KC_USER, blob("token-1-new"))
        st = state.default_state()
        st["settings"] = {"language": "en"}
        st["slots"]["1"] = _registered_slot(1, **slot_extra)
        st["slots"]["2"] = _registered_slot(2)
        st["activeSlot"] = 2
        state.save_state(st)

    def assertSettingsKept(self, slot):
        for key, value in USER_SETTINGS.items():
            self.assertEqual(slot.get(key), value, key)

    def test_add_keeps_alias_thresholds_and_weekly_max(self):
        with temp_home(), fake_store(paths.HOME) as store:
            self._world(store, dict(USER_SETTINGS, enabled=False,
                                    quarantine={"reason": "invalid_grant"}))
            rc, out, err = run_cli("add")
            st = state.load_state()
            slot = state.slot_of(st, 1)
            lim = limits.slot_limits(st, slot)
            stored = store.read(paths.BACKUP_SERVICE, "slot-1")
        self.assertEqual(rc, 0, err)
        self.assertIn("Slot 1 updated", out + err)          # '추가'가 아니라 '갱신'
        self.assertIn("per-account limits kept", out + err)
        self.assertNotIn("added", out + err)
        self.assertSettingsKept(slot)
        self.assertEqual((lim["session"], lim["weekly"], lim["cap"]), (80.0, 70.0, 70.0))
        self.assertEqual(slot["addedAt"], "2026-01-02T03:04:05+00:00")   # 처음 등록한 시각 유지
        self.assertIsNone(slot["quarantine"])                # 다시 로그인 → 격리 해제
        self.assertTrue(slot["enabled"])                     # 다시 등록 = 쓰겠다는 뜻
        self.assertEqual(st["activeSlot"], 1)
        self.assertIn("token-1-new", stored)                 # 자격증명은 새 것으로

    def test_add_of_a_new_account_still_says_added(self):
        with temp_home(), fake_store(paths.HOME) as store:
            self._world(store, {})
            st = state.load_state()
            del st["slots"]["1"]
            state.save_state(st)
            rc, out, err = run_cli("add")
        self.assertEqual(rc, 0, err)
        self.assertIn("Slot 1 added: user1@example.com", out + err)

    def test_add_message_in_korean(self):
        with temp_home(), fake_store(paths.HOME) as store:
            self._world(store, dict(USER_SETTINGS))
            rc, out, err = run_cli("add", lang="ko")
        self.assertEqual(rc, 0, err)
        self.assertIn("슬롯 1 갱신", out + err)
        self.assertIn("계정별 한도는 그대로", out + err)

    def test_browser_login_registration_keeps_settings(self):
        st = state.default_state()
        st["slots"]["1"] = _registered_slot(1, **USER_SETTINGS)
        st["slots"]["2"] = _registered_slot(2)
        st["activeSlot"] = 2
        with (mock.patch.object(accounts, "read_live_oauth_account", return_value=None),
              mock.patch.object(creds, "kc_write_slot") as write,
              mock.patch.object(state, "save_state")):
            n, label, is_new = accounts.register_oauth_slot(st, blob("token-1-new"), identity(1))
        self.assertEqual((n, label, is_new), (1, "work", False))
        self.assertSettingsKept(st["slots"]["1"])
        write.assert_called_once()

    def test_browser_login_with_a_new_alias_keeps_the_limits(self):
        st = state.default_state()
        st["slots"]["1"] = _registered_slot(1, **USER_SETTINGS)
        st["activeSlot"] = None
        with (mock.patch.object(accounts, "read_live_oauth_account", return_value=None),
              mock.patch.object(creds, "kc_write_slot"),
              mock.patch.object(state, "save_state")):
            _n, label, _new = accounts.register_oauth_slot(st, blob(), identity(1), alias="renamed")
        slot = st["slots"]["1"]
        self.assertEqual(label, "renamed")                   # 직접 준 별칭이 이긴다
        self.assertEqual((slot["sessionThreshold"], slot["weeklyThreshold"], slot["weeklyCap"]),
                         (80.0, 75.0, 70.0))

    def test_dashboard_register_current_login_says_updated(self):
        with temp_home(), fake_store(paths.HOME) as store:
            self._world(store, dict(USER_SETTINGS))
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            httpd.api_token = "t" * 20
            thread = threading.Thread(target=httpd.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
                conn.request("POST", "/api/add/current", body=b"{}", headers={
                    "Host": f"127.0.0.1:{httpd.server_address[1]}", "X-LimitPilot": "1",
                    "X-LimitPilot-Token": "t" * 20, "Content-Type": "application/json"})
                resp = conn.getresponse()
                payload = json.loads(resp.read())
                conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
            slot = state.slot_of(state.load_state(), 1)
        self.assertEqual(resp.status, 200, payload)
        self.assertIn("Slot 1 updated", payload["message"])
        self.assertSettingsKept(slot)

    def test_gateway_registered_again_keeps_the_alias_the_user_chose(self):
        st = state.default_state()
        url, model = "https://gateway.example.invalid", "example-model"
        with mock.patch.object(creds, "kc_write_slot"), mock.patch.object(state, "save_state"):
            n, alias, is_new = gateway.register_gateway_slot(st, url, model, "tok-1")
            self.assertEqual((alias, is_new), (model, True))   # 새 게이트웨이: 모델 이름
            accounts.set_slot_alias(st, n, "My gateway")
            again = gateway.register_gateway_slot(st, url, model, "tok-2")
            explicit = gateway.register_gateway_slot(st, url, model, "tok-3", alias="Other")
        self.assertEqual(again, (n, "My gateway", False))     # 별칭을 안 주면 예전 별칭
        self.assertEqual(explicit, (n, "Other", False))       # 직접 주면 그 별칭

    def test_practice_mode_end_to_end_like_the_qa_report(self):
        """`limits 1 --session 80 --cap 70` → `add` → `limits 1` still shows 80 / 70."""
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, PYTHONPATH=SRC, PYTHONDONTWRITEBYTECODE="1",
                       LIMITPILOT_DEMO_ROOT=os.path.join(tmp, "limitpilot-demo"))
            for key in ("LIMITPILOT_HOME", "LIMITPILOT_CLAUDE_HOME", "LIMITPILOT_LANG"):
                env.pop(key, None)

            def lp(*argv):
                res = subprocess.run([sys.executable, "-B", "-m", "limitpilot", "--demo",
                                      "--lang", "en", *argv], capture_output=True, text=True,
                                     env=env, cwd=tmp, timeout=120)
                return res.returncode, res.stdout + res.stderr

            self.assertEqual(lp("limits", "1", "--session", "80", "--cap", "70")[0], 0)
            rc, out = lp("add")
            self.assertEqual(rc, 0, out)
            self.assertIn("Slot 1 updated", out)
            rc, out = lp("limits", "1")
            self.assertIn("session threshold 80%", out)
            self.assertIn("weekly max 70%", out)
            rc, out = lp("list")
            self.assertIn("Slot 1 (active) [limits session 80", out)
            self.assertIn(": work ", out)                        # 별칭도 그대로


# ---------------------------------------------------------------- round 2 (QA Q-2)


class GatewayEnvFileTests(unittest.TestCase):
    def test_missing_env_file_says_what_to_write_and_where(self):
        with temp_home() as root:
            missing = os.path.join(root, "gateway.local.env")
            rc, out, err = run_cli("gateway-add", "--env-file", missing)
        text = out + err
        self.assertEqual(rc, 1)
        self.assertIn(missing, text)
        self.assertIn("does not exist", text)
        for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_AUTH_TOKEN",
                    "GATEWAY_AUTH_MODE=api-key", "ANTHROPIC_API_KEY", "GATEWAY_ALIAS", "--env-file"):
            self.assertIn(key, text)
        self.assertNotIn("Errno", text)
        self.assertNotIn("Traceback", text)
        self.assertIsNone(HANGUL.search(text))

    def test_missing_env_file_in_korean(self):
        with temp_home() as root:
            rc, out, err = run_cli("gateway-add", "--env-file",
                                   os.path.join(root, "nope.env"), lang="ko")
        self.assertEqual(rc, 1)
        self.assertIn("파일", out + err)
        self.assertIn("--env-file", out + err)

    def test_missing_keys_are_named_and_values_never_printed(self):
        with temp_home() as root:
            path = os.path.join(root, "partial.env")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("ANTHROPIC_BASE_URL=https://gateway.example.invalid\n"
                             "ANTHROPIC_AUTH_TOKEN=secret-value-123\n")
            rc, out, err = run_cli("gateway-add", "--env-file", path)
            self.assertEqual(state.load_state()["slots"], {})   # 아무것도 등록하지 않음
        self.assertEqual(rc, 1)
        self.assertIn("ANTHROPIC_MODEL", out + err)
        self.assertNotIn("secret-value-123", out + err)


# ---------------------------------------------------------------- round 2 (QA Q-4)


class PracticeLoginAndKeepaliveTests(unittest.TestCase):
    """In practice mode `login` and `keepalive` say why nothing real happens and exit 0."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {"LIMITPILOT_DEMO": "1"})
        env.start()
        self.addCleanup(env.stop)

    def test_login_explains_practice_mode_without_starting_claude(self):
        for lang, phrase in (("en", "Practice mode"), ("ko", "연습 모드")):
            with temp_home(), mock.patch.object(session, "ClaudeLoginSession") as login:
                rc, out, err = run_cli("login", lang=lang)
            self.assertEqual(rc, 0, err)
            self.assertIn(phrase, out)
            self.assertIn("claude auth login", out)
            self.assertNotIn("terminal", out + err)
            login.assert_not_called()

    def _two_slots(self):
        st = state.default_state()
        st["slots"]["1"] = _registered_slot(1)
        st["slots"]["2"] = _registered_slot(2)
        st["activeSlot"] = 1
        state.save_state(st)

    def test_keepalive_lists_the_due_slots_sends_nothing_and_records_nothing(self):
        for lang, phrase in (("en", "nothing was sent"), ("ko", "보낸 것 없음")):
            with (temp_home(), mock.patch.object(usage_api, "send_keepalive") as send,
                  mock.patch.object(collector, "keepalive_slot") as run_one):
                self._two_slots()
                rc, out, err = run_cli("keepalive", lang=lang)
                slots = state.load_state()["slots"]
            self.assertEqual(rc, 0, err)
            self.assertIn(phrase, out)
            self.assertIn("1, 2", out)                        # 대상 고르기는 실제 코드
            send.assert_not_called()
            run_one.assert_not_called()
            self.assertFalse(any("keepalive" in s for s in slots.values()))   # 실패 기록 없음

    def test_dashboard_browser_add_explains_practice_mode(self):
        with temp_home(), mock.patch.object(session, "ClaudeLoginSession") as login:
            with self.assertRaises(server.LimitPilotError) as caught:
                server.start_add("alias")
        self.assertIn("claude auth login", str(caught.exception))
        login.assert_not_called()

    def test_dashboard_turns_a_blocked_call_into_a_practice_message(self):
        blocked = sandbox.SandboxViolation("sandbox blocked network: request gateway.example.invalid")
        body = {"baseUrl": "https://gateway.example.invalid", "modelId": "m",
                "token": "tok-secret-9"}
        with temp_home(), mock.patch.object(gateway, "probe_gateway_connection",
                                            side_effect=blocked):
            st = state.default_state()
            st["settings"] = {"language": "en"}
            state.save_state(st)
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            httpd.api_token = "t" * 20
            thread = threading.Thread(target=httpd.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
                conn.request("POST", "/api/gateway/add", body=json.dumps(body).encode(), headers={
                    "Host": f"127.0.0.1:{httpd.server_address[1]}", "X-LimitPilot": "1",
                    "X-LimitPilot-Token": "t" * 20, "Content-Type": "application/json"})
                resp = conn.getresponse()
                payload = json.loads(resp.read())
                conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
        self.assertEqual(resp.status, 400)
        self.assertIn("Blocked in practice mode", payload["error"])   # 'internal' 이 아니라 이유
        self.assertNotIn("tok-secret-9", payload["error"])


class DashboardBrowserOpenTests(unittest.TestCase):
    """`dashboard` without --no-open: practice mode prints the address instead of being blocked,
    and a browser that cannot start never leaves the server socket or its run record behind."""

    def _run_main(self, practice, open_error=None):
        saved_scan = server.status_mod._BACKGROUND_SCAN
        saved_sigterm = signal.getsignal(signal.SIGTERM)
        out, err = io.StringIO(), io.StringIO()
        raised = None
        try:
            with (temp_home(), mock.patch.dict(os.environ), mock.patch.object(i18n, "_override", "en"),
                  mock.patch.object(server, "port_in_use", return_value=False),
                  mock.patch.object(server.deploy, "maybe_refresh_daemon_copy"),
                  mock.patch.object(server, "open_ui", side_effect=open_error) as open_ui,
                  mock.patch.object(server.ThreadingHTTPServer, "serve_forever"),
                  mock.patch.object(server.ThreadingHTTPServer, "server_close",
                                    autospec=True, side_effect=server.ThreadingHTTPServer.server_close) as close,
                  contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)):
                os.environ.pop("LIMITPILOT_DEMO", None)
                if practice:
                    os.environ["LIMITPILOT_DEMO"] = "1"
                try:
                    rc = server.main(["--port", "0"])
                except RuntimeError as exc:
                    rc, raised = None, exc
                records = [name for name in os.listdir(paths.STATE_DIR)
                           if name.startswith(paths.DASHBOARD_RUNTIME_PREFIX)]
        finally:
            server.status_mod._BACKGROUND_SCAN = saved_scan
            signal.signal(signal.SIGTERM, saved_sigterm)
        return rc, raised, out.getvalue(), err.getvalue(), open_ui, close, records

    def test_practice_mode_prints_the_address_and_starts_no_program(self):
        rc, _raised, out, _err, open_ui, close, records = self._run_main(practice=True)
        self.assertEqual(rc, 0)
        self.assertIn("Practice mode: open http://127.0.0.1:0/", out)
        open_ui.assert_not_called()
        close.assert_called()
        self.assertEqual(records, [])

    def test_browser_that_cannot_start_does_not_stop_the_dashboard(self):
        rc, _raised, _out, err, open_ui, close, records = self._run_main(
            practice=False, open_error=FileNotFoundError(2, "No such file or directory"))
        self.assertEqual(rc, 0)
        self.assertIn("open http://127.0.0.1:0/ yourself", err)
        open_ui.assert_called_once()
        close.assert_called()
        self.assertEqual(records, [])

    def test_unexpected_open_error_still_cleans_up(self):
        rc, raised, _out, _err, _open_ui, close, records = self._run_main(
            practice=False, open_error=RuntimeError("boom"))
        self.assertIsNotNone(raised)
        close.assert_called()                                # 소켓을 닫았다
        self.assertEqual(records, [])                        # 실행 기록도 지웠다


class KeepaliveIoBoundaryTests(unittest.TestCase):
    """The keep-alive message is an I/O boundary like the usage fetch: practice mode answers it,
    and a request the sandbox blocks is never recorded as the account's failure."""

    def tearDown(self):
        usage_api.set_source(None)

    def test_practice_source_answers_without_sending(self):
        scenario = demo.load_scenario()
        usage_api.set_source(demo.ScenarioSource(scenario, 0.0))
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertEqual(usage_api.send_keepalive("demo-access-1"), (True, None, None))
            self.assertEqual(usage_api.send_keepalive("unknown")[:2], (False, "http-401"))
        urlopen.assert_not_called()

    def test_practice_tick_records_a_simulated_ok(self):
        scenario = demo.load_scenario()
        usage_api.set_source(demo.ScenarioSource(scenario, 0.0))
        st = state.default_state()
        st["slots"]["1"] = _registered_slot(1)
        st["activeSlot"] = 1
        live = {"accessToken": "demo-access-1"}
        with (mock.patch.object(collector, "slot_access_oauth", return_value=(live, None)),
              mock.patch("limitpilot.logutil.log")):
            rec = collector.keepalive_slot(st, 1, now=1000.0)
        self.assertTrue(rec["ok"])
        self.assertTrue(st["slots"]["1"]["keepalive"]["ok"])

    def test_sandbox_block_is_not_swallowed_as_error(self):
        usage_api.set_source(None)   # 실제 HTTP 경로(urlopen 은 아래에서 막힌 것으로 흉내)
        blocked = sandbox.SandboxViolation("sandbox blocked network: request api.example.invalid")
        with mock.patch("urllib.request.urlopen", side_effect=blocked):
            with self.assertRaises(sandbox.SandboxViolation):
                usage_api.send_keepalive("token")


if __name__ == "__main__":
    unittest.main()
