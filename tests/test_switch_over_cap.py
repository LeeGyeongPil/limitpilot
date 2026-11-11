"""Manual switch to a slot over its weekly max: warn + exit 3, --force proceeds (ko and en)."""

import contextlib
import http.client
import io
import json
import os
import threading
import unittest
from datetime import datetime, timezone, timedelta
from unittest import mock

from limitpilot import cli, i18n, locks, state
from limitpilot.dashboard import server
from limitpilot.errors import LimitPilotError
from limitpilot.policy import limits
from limitpilot import switch as switch_mod


def _iso(delta_s):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slot(cap=30, pct=41, resets_in=3 * 86400, **extra):
    s = {"email": "t@example.com", "enabled": True, "kind": "oauth",
         "oauthAccount": {"emailAddress": "t@example.com"}}
    if cap is not None:
        s["weeklyCap"] = cap
    if pct is not None:
        s["usage"] = {"seven_day": {"pct": pct, "resets_at": _iso(resets_in)}}
    s.update(extra)
    return s


def _state(slot):
    st = state.default_state()
    st.setdefault("settings", {})["finalBurn"] = True  # opt-in setting: cap is lifted near the weekly reset
    st["slots"]["1"] = {"email": "a@example.com", "enabled": True, "kind": "oauth"}
    st["slots"]["2"] = slot
    st["activeSlot"] = 1
    return st


class LimitsFunctionTests(unittest.TestCase):
    def test_over_cap_returns_pct_and_cap(self):
        s = _slot()
        self.assertEqual(limits.manual_switch_over_cap(_state(s), s), (41.0, 30.0))

    def test_no_warning_cases(self):
        cases = (_slot(cap=100), _slot(pct=None), _slot(pct=20),
                 _slot(resets_in=-3600),   # stale window
                 _slot(resets_in=3600),    # final burn lifts the cap
                 _slot(kind="gateway"))
        for s in cases:
            self.assertIsNone(limits.manual_switch_over_cap(_state(s), s), s)
        bad = _slot()
        bad["usage"] = "garbage"
        self.assertIsNone(limits.manual_switch_over_cap(_state(bad), bad))
        self.assertIsNone(limits.manual_switch_over_cap(_state(_slot()), None))


class _Base(unittest.TestCase):
    LANG = "ko"

    def setUp(self):
        self._old = i18n.current_lang()
        i18n.set_lang(self.LANG)
        patcher = mock.patch.dict(os.environ, {"LIMITPILOT_LANG": self.LANG})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(i18n.set_lang, self._old)

    def run_switch(self, slot, force):
        st = _state(slot)
        argv = ["switch", "2"] + (["--force"] if force else [])
        args = cli.build_parser().parse_args(argv)
        buf = io.StringIO()
        with (mock.patch.object(locks, "state_lock", return_value=contextlib.nullcontext()),
              mock.patch.object(state, "load_state", return_value=st),
              mock.patch.object(state, "save_state"),
              mock.patch.object(cli.accounts, "reconcile_active_slot"),
              mock.patch.object(cli.notify_mod, "notify"),
              mock.patch.object(switch_mod, "perform_switch", return_value="switched") as sw,
              contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf)):
            rc = cli.cmd_switch(args)
        return rc, buf.getvalue(), sw, st


class CliKoTests(_Base):
    def test_blocked_by_default(self):
        rc, out, sw, st = self.run_switch(_slot(), False)
        self.assertEqual(rc, 3)
        sw.assert_not_called()
        self.assertEqual(st["activeSlot"], 1)
        self.assertIn("주간 최대치를 넘었습니다", out)
        self.assertNotIn("Traceback", out)

    def test_force_switches_with_warning(self):
        rc, out, sw, _ = self.run_switch(_slot(), True)
        self.assertEqual(rc, 0)
        sw.assert_called_once()
        self.assertIn("경고", out)

    def test_no_warning_without_cap_or_data(self):
        for s in (_slot(cap=100), _slot(pct=None), _slot(resets_in=-60), _slot(resets_in=60)):
            rc, out, sw, _ = self.run_switch(s, False)
            self.assertEqual(rc, 0)
            self.assertNotIn("주간 최대치", out)


class CliEnTests(_Base):
    LANG = "en"

    def test_blocked_by_default(self):
        rc, out, sw, _ = self.run_switch(_slot(), False)
        self.assertEqual(rc, 3)
        sw.assert_not_called()
        self.assertIn("over its weekly max", out)
        self.assertIn("--force", out)
        self.assertNotIn("Traceback", out)

    def test_force_switches_with_warning(self):
        rc, out, sw, _ = self.run_switch(_slot(), True)
        self.assertEqual(rc, 0)
        self.assertIn("Warning", out)
        self.assertIn("switching anyway", out)

    def test_message_keys_in_both_languages(self):
        for key in ("switch_over_cap", "switch_over_cap_forced"):
            self.assertIn(key, i18n.MESSAGES["ko"])
            self.assertIn(key, i18n.MESSAGES["en"])


class DashboardTests(_Base):
    def _activate(self, force):
        st = _state(_slot())
        with (mock.patch.object(locks, "state_lock", return_value=contextlib.nullcontext()),
              mock.patch.object(state, "load_state", return_value=st),
              mock.patch.object(switch_mod, "perform_switch", return_value="switched") as sw):
            try:
                return server.mutate_slot(2, "activate", force=force), sw
            except LimitPilotError as e:
                return e, sw

    def test_without_force_raises_over_cap(self):
        err, sw = self._activate(False)
        self.assertIsInstance(err, server.OverCapError)
        sw.assert_not_called()

    def test_with_force_switches(self):
        msg, sw = self._activate(True)
        sw.assert_called_once()
        self.assertIn("활성화 완료", msg)

    def test_over_cap_ok_overrides_cap_only(self):
        # unmanaged live login sends force=True but must still get the over-cap prompt
        st = _state(_slot())
        with (mock.patch.object(locks, "state_lock", return_value=contextlib.nullcontext()),
              mock.patch.object(state, "load_state", return_value=st),
              mock.patch.object(switch_mod, "perform_switch", return_value="switched") as sw):
            with self.assertRaises(server.OverCapError):
                server.mutate_slot(2, "activate", force=True, over_cap_ok=False)
            sw.assert_not_called()
            server.mutate_slot(2, "activate", force=True, over_cap_ok=True)
            sw.assert_called_once()


class DashboardHttpTests(_Base):
    """HTTP layer: 409 + code over_cap, then 200 once the user confirms."""
    TOKEN = "over-cap-test-token"

    def setUp(self):
        super().setUp()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.httpd.api_token = self.TOKEN
        self.httpd.shutdown_nonce = "n" * 32
        self.httpd.instance_id = "i" * 24
        self.port = self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()

        def stop():
            self.httpd.shutdown()
            self.httpd.server_close()
            thread.join(timeout=5)
        self.addCleanup(stop)

    def post(self, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": f"127.0.0.1:{self.port}", "X-LimitPilot-Token": self.TOKEN,
                   "X-LimitPilot": "1", "Origin": f"http://127.0.0.1:{self.port}",
                   "Content-Type": "application/json"}
        conn.request("POST", "/api/slot/2/activate", body=json.dumps(body).encode(), headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_409_then_confirmed(self):
        st = _state(_slot())
        with (mock.patch.object(locks, "state_lock", return_value=contextlib.nullcontext()),
              mock.patch.object(state, "load_state", return_value=st),
              mock.patch.object(switch_mod, "perform_switch", return_value="switched") as sw):
            code, data = self.post({"force": False})
            self.assertEqual(code, 409)
            self.assertEqual(data["code"], "over_cap")
            self.assertIn("주간 최대치", data["error"])
            sw.assert_not_called()
            code, data = self.post({"force": False, "overCapOk": True})
            self.assertEqual(code, 200)
            sw.assert_called_once()

    def test_unmanaged_force_still_prompts(self):
        st = _state(_slot())
        with (mock.patch.object(locks, "state_lock", return_value=contextlib.nullcontext()),
              mock.patch.object(state, "load_state", return_value=st),
              mock.patch.object(switch_mod, "perform_switch", return_value="switched")):
            code, data = self.post({"force": True, "overCapOk": False})
            self.assertEqual((code, data["code"]), (409, "over_cap"))

    def test_bad_over_cap_ok_type_is_400(self):
        code, data = self.post({"overCapOk": "yes"})
        self.assertEqual(code, 400)


def _auto_state():
    """Active slot 1; slot 2 over its cap (known usage); optional slot 3 unknown."""
    st = _state(_slot())
    return st


class AutoPickTests(_Base):
    """`switch` without a number must not pick an over-cap / exhausted slot (regression)."""

    def run_auto(self, st):
        args = cli.build_parser().parse_args(["switch"])
        buf = io.StringIO()
        with (mock.patch.object(locks, "state_lock", return_value=contextlib.nullcontext()),
              mock.patch.object(state, "load_state", return_value=st),
              mock.patch.object(state, "save_state"),
              mock.patch.object(cli.accounts, "reconcile_active_slot"),
              mock.patch.object(cli.notify_mod, "notify"),
              mock.patch.object(switch_mod, "perform_switch", return_value="switched") as sw,
              contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf)):
            rc = cli.cmd_switch(args)
        return rc, buf.getvalue(), sw

    def test_only_over_cap_candidate_is_not_picked(self):
        rc, out, sw = self.run_auto(_auto_state())
        self.assertEqual(rc, 1)
        sw.assert_not_called()
        self.assertIn("주간 최대치를 넘었습니다", out)
        self.assertNotIn("Traceback", out)

    def test_other_candidate_is_preferred(self):
        st = _auto_state()
        st["slots"]["3"] = _slot(cap=None, pct=50)
        rc, out, sw = self.run_auto(st)
        self.assertEqual(rc, 0)
        self.assertEqual(sw.call_args[0][1], 3)

    def test_unknown_usage_slot_is_still_a_fallback(self):
        st = _auto_state()
        st["slots"]["3"] = _slot(cap=None, pct=None)
        rc, out, sw = self.run_auto(st)
        self.assertEqual(rc, 0)
        self.assertEqual(sw.call_args[0][1], 3)


class AutoPickEnTests(AutoPickTests):
    LANG = "en"

    def test_only_over_cap_candidate_is_not_picked(self):
        rc, out, sw = self.run_auto(_auto_state())
        self.assertEqual(rc, 1)
        sw.assert_not_called()
        self.assertIn("weekly max", out)


if __name__ == "__main__":
    unittest.main()
