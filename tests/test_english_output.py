"""AC-7 (i18n): with ``--lang en`` nothing the user can see contains Hangul.

Two layers:
* a static check — no Hangul string literal outside ``i18n.py`` / ``i18n_more.py``, except literals
  that sit next to their English twin (``_r(ko, en)``, ``tr(lang, ko, en)``, ``(ko, en)`` tuples,
  ``en if lang == "en" else ko``, ``{"ko": ..., "en": ...}`` tables);
* runtime samples of the paths that used to leak Korean: daemon/auto tick log lines, switch,
  gateway, purge, refresh, keychain and login errors and the dashboard HTTP error bodies.
"""

import ast
import contextlib
import http.client
import io
import json
import os
import re
import subprocess
import threading
import unittest
from unittest import mock

from limitpilot import cli
from limitpilot import collector
from limitpilot import gateway
from limitpilot import i18n
from limitpilot import logutil
from limitpilot import paths
from limitpilot import purge
from limitpilot import state
from limitpilot import switch
from limitpilot import tick
from limitpilot.dashboard import server
from limitpilot.errors import KeychainError, LimitPilotError, SwitchError
from limitpilot.login import session
from limitpilot.store import keychain
from tests.support import (FakeUsage, SRC, blob, fake_store, identity, temp_home, usage,
                           usage_source)

HANGUL = re.compile("[ᄀ-ᇿ㄰-㆏가-힣]")
# Files whose whole job is to hold the message tables.
TABLE_FILES = {"i18n.py", "i18n_more.py"}
# Extra allowed literals (file name -> reason). Empty on purpose: add a line here, with a reason,
# rather than leaving Korean text in a code path.
ALLOWED: dict = {}


def _is_text(node) -> bool:
    return isinstance(node, ast.JoinedStr) or (
        isinstance(node, ast.Constant) and isinstance(node.value, str))


def _has_hangul(node) -> bool:
    return any(isinstance(n, ast.Constant) and isinstance(n.value, str) and HANGUL.search(n.value)
               for n in ast.walk(node))


def hangul_literals_outside_tables(root: str) -> list:
    """(file, line, text) for every Hangul string literal that has no English twin."""
    found = []
    for base, _dirs, files in os.walk(root):
        for name in sorted(files):
            if not name.endswith(".py") or name in TABLE_FILES:
                continue
            path = os.path.join(base, name)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            covered = set()
            for node in ast.walk(tree):
                paired = []
                if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if (node.body and isinstance(node.body[0], ast.Expr)
                            and isinstance(node.body[0].value, ast.Constant)):
                        covered.add(id(node.body[0].value))  # docstring
                elif isinstance(node, ast.Call):
                    func = node.func
                    called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                    if called in ("_r", "tr") and len(node.args) >= 2:
                        paired = node.args
                elif isinstance(node, ast.Tuple):
                    if (len(node.elts) == 2 and all(_is_text(e) for e in node.elts)
                            and sum(_has_hangul(e) for e in node.elts) == 1):
                        paired = node.elts
                elif isinstance(node, ast.IfExp):
                    if (_is_text(node.body) and _is_text(node.orelse)
                            and _has_hangul(node.body) != _has_hangul(node.orelse)):
                        paired = [node.body, node.orelse]
                elif isinstance(node, ast.Dict):
                    keys = {getattr(k, "value", None) for k in node.keys}
                    if {"ko", "en"} <= keys:
                        paired = node.values
                for part in paired:
                    covered.update(id(n) for n in ast.walk(part))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and HANGUL.search(node.value) and id(node) not in covered
                        and name not in ALLOWED):
                    found.append((os.path.relpath(path, root), node.lineno, node.value[:60]))
    return found


class StaticTests(unittest.TestCase):
    def test_no_untranslated_hangul_literals_in_the_package(self):
        offenders = hangul_literals_outside_tables(os.path.join(SRC, "limitpilot"))
        self.assertEqual(offenders, [], "move these to i18n_more.py: " + repr(offenders[:5]))

    def test_the_checker_catches_a_leak(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "leak.py"), "w", encoding="utf-8") as handle:
                handle.write('def f():\n    raise ValueError("한글")\n'
                             'def g(en):\n    return "ok" if en else "한글"\n')
            leaks = hangul_literals_outside_tables(tmp)
        self.assertEqual([(f, line) for f, line, _ in leaks], [("leak.py", 2)])


class _English(unittest.TestCase):
    """Run every test with the language forced to English; restore afterwards."""

    def setUp(self):
        self._saved = i18n._override
        i18n.set_lang("en")
        self._env = mock.patch.dict(os.environ, {"LIMITPILOT_LANG": "en"})
        self._env.start()
        self._home = temp_home()
        self._home.__enter__()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self._home.__exit__(None, None, None)
        self._env.stop()
        i18n._override = self._saved

    def assertEnglish(self, text, what=""):
        self.assertIsNone(HANGUL.search(str(text)), f"Hangul in {what}: {text!r}")


class TickLogTests(_English):
    def _world(self, store, active_pct):
        with open(paths.CLAUDE_JSON, "w", encoding="utf-8") as handle:
            json.dump({"oauthAccount": identity(1)}, handle)
        store.write(paths.ACTIVE_SERVICE, paths.KC_USER, blob("token-1"))
        store.write(paths.BACKUP_SERVICE, "slot-1", blob("token-1"))
        store.write(paths.BACKUP_SERVICE, "slot-2", blob("token-2"))
        st = state.default_state()
        st["activeSlot"] = 1
        for n in (1, 2):
            st["slots"][str(n)] = {"email": f"user{n}@example.com", "oauthAccount": identity(n),
                                   "enabled": True, "kind": "oauth"}
        state.save_state(st)
        return FakeUsage({"token-1": usage(session=active_pct, weekly=20),
                          "token-2": usage(session=5, weekly=10)})

    def _lines_of_tick(self, active_pct, **kw):
        lines = []
        with fake_store(paths.HOME) as store:
            src = self._world(store, active_pct)
            with (usage_source(src),
                  mock.patch.object(tick.notify_mod, "notify") as notify,
                  mock.patch.object(tick.notify_mod, "notify_once") as notify_once,
                  mock.patch("limitpilot.logutil.log", side_effect=lines.append),
                  mock.patch("limitpilot.logutil.error", side_effect=lines.append)):
                tick.run_tick(**kw)
        notices = [c.args for c in notify.call_args_list + notify_once.call_args_list]
        return lines, notices

    def test_dry_run_switch_line_is_english(self):
        lines, _ = self._lines_of_tick(95, dry_run=True)
        self.assertTrue(any("Auto switch" in line for line in lines), lines)
        for line in lines:
            self.assertEnglish(line, "tick log")

    def test_suggestion_and_no_action_lines_are_english(self):
        lines, notices = self._lines_of_tick(95)
        self.assertTrue(any("Switch suggested" in line for line in lines), lines)
        lines2, _ = self._lines_of_tick(10)
        self.assertTrue(any("No action" in line for line in lines2), lines2)
        for text in lines + lines2 + [str(n) for n in notices]:
            self.assertEnglish(text, "tick log or notification")

    def test_every_decision_reason_is_english(self):
        from limitpilot.constants import reason_all_exhausted
        self.assertEnglish(reason_all_exhausted(), "blocked reason")

    def test_unexpected_error_line_is_english(self):
        lines = []
        with (mock.patch.object(tick.state, "load_state", side_effect=RuntimeError("boom")),
              mock.patch("limitpilot.logutil.error", side_effect=lines.append)):
            tick.run_tick()
        self.assertEqual(len(lines), 1)
        self.assertEnglish(lines[0], "unexpected tick error")

    def test_collector_log_lines_are_english(self):
        lines = []
        with (fake_store(paths.HOME), mock.patch("limitpilot.logutil.log", side_effect=lines.append),
              mock.patch.object(collector.notify, "notify_once") as notice):
            st = state.default_state()
            st["slots"]["1"] = {"email": "user1@example.com", "enabled": True}
            collector.quarantine(st, 1, "token revoked")
        self.assertTrue(lines)
        for line in lines + [str(notice.call_args)]:
            self.assertEnglish(line, "quarantine log or notification")


class ErrorMessageTests(_English):
    def test_switch_errors(self):
        st = state.default_state()
        st["slots"]["1"] = {"email": "a@example.com", "enabled": False, "kind": "oauth",
                            "oauthAccount": identity(1)}
        st["slots"]["2"] = {"email": "b@example.com", "enabled": True, "kind": "setup-token"}
        st["slots"]["3"] = {"email": "c@example.com", "enabled": True, "kind": "oauth",
                            "quarantine": {"reason": "x"}}
        st["slots"]["4"] = {"email": "d@example.com", "enabled": True, "kind": "oauth"}
        for target in (9, 1, 2, 3, 4):
            with self.assertRaises(SwitchError) as ctx:
                switch.perform_switch(st, target)
            self.assertEnglish(ctx.exception, f"switch slot {target}")

    def test_gateway_validation_errors(self):
        bad_inputs = [
            (1, "m", "t"), ("http://x.example", "m", "t"), ("https://x.example", "", "t"),
            ("https://x.example", "m", "t", "weird"), ("https://x.example", "m", ""),
            ("https://x.example", "m", "t", "bearer", "a" * 200),
        ]
        for args in bad_inputs:
            with self.assertRaises(LimitPilotError) as ctx:
                gateway.validate_gateway_profile(*args)
            self.assertEnglish(ctx.exception, repr(args))
        for bad in (True, "x", -1, 1.5):
            with self.assertRaises(LimitPilotError) as ctx:
                gateway.validate_gateway_usage_config(input_token_limit=bad)
            self.assertEnglish(ctx.exception, repr(bad))
        with self.assertRaises(LimitPilotError) as ctx:
            gateway.validate_gateway_usage_config(output_price_per_million=-1)
        self.assertEnglish(ctx.exception, "negative price")

    def test_gateway_settings_file_errors(self):
        os.makedirs(os.path.dirname(paths.CLAUDE_SETTINGS_FILE), exist_ok=True)
        with open(paths.CLAUDE_SETTINGS_FILE, "w", encoding="utf-8") as handle:
            handle.write("[1, 2]")
        with self.assertRaises(SwitchError) as ctx:
            gateway._read_claude_settings()
        self.assertEnglish(ctx.exception, "settings top level")
        with open(paths.CLAUDE_SETTINGS_FILE, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        with self.assertRaises(SwitchError) as ctx:
            gateway._read_claude_settings()
        self.assertEnglish(ctx.exception, "settings unreadable")

    def test_refresh_messages(self):
        cases = [("ok", None), ("recent", None), ("recent-failed", None), ("backoff", 0),
                 ("skip", None), ("error", "unmanaged"), ("error", "token-expired"),
                 ("error", "no-credential"), ("error", "keychain"), ("error", "keychain-write"),
                 ("error", "quarantined"), ("error", "refresh-transient"), ("error", "http-500")]
        for status, detail in cases:
            self.assertEnglish(collector.refresh_message(1, status, detail, "en"), status)
        self.assertIn("슬롯", collector.refresh_message(1, "ok", None, "ko"))

    def test_keychain_errors(self):
        timeout = subprocess.TimeoutExpired("security", 1)
        with mock.patch("subprocess.run", side_effect=timeout):
            for call in (lambda: keychain.kc_read("svc", "acct"),
                         lambda: keychain.kc_write("svc", "acct", "x"),
                         lambda: keychain.kc_delete("svc", "acct", attempts=1)):
                with self.assertRaises(KeychainError) as ctx:
                    call()
                self.assertEnglish(ctx.exception, "keychain timeout")
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="denied")
        with mock.patch("subprocess.run", return_value=failed):
            with self.assertRaises(KeychainError) as ctx:
                keychain.kc_write("svc", "acct", "x")
            self.assertEnglish(ctx.exception, "keychain write")

    def test_login_validation_errors(self):
        broken = [
            ("not json", None),
            (json.dumps({"claudeAiOauth": {"accessToken": ""}}), None),
            (json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": ""}}), None),
            (json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r"}}), None),
            (json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r",
                                           "expiresAt": 1, "scopes": ["x"]}}), None),
        ]
        for text, profile in broken:
            with self.assertRaises(LimitPilotError) as ctx:
                session.validate_login_credentials(text, profile)
            self.assertEnglish(ctx.exception, text)

    def test_login_session_errors(self):
        sess = session.ClaudeLoginSession()
        with self.assertRaises(LimitPilotError) as ctx:
            sess.feed("")
        self.assertEnglish(ctx.exception, "empty code")
        with self.assertRaises(LimitPilotError) as ctx:
            sess.feed("x" * 5000)
        self.assertEnglish(ctx.exception, "long code")
        with self.assertRaises(LimitPilotError) as ctx:
            sess.feed("abc")
        self.assertEnglish(ctx.exception, "closed session")


class PurgeOutputTests(_English):
    def test_cancelled_purge_prints_english(self):
        err = io.StringIO()
        with (mock.patch("builtins.input", return_value="no"),
              mock.patch.object(purge, "_validate_state_root_for_purge", return_value=paths.STATE_DIR),
              contextlib.redirect_stderr(err)):
            rc = purge.cmd_purge(mock.Mock(yes=False))
        self.assertEqual(rc, 0)
        self.assertIn("Purge cancelled", err.getvalue())
        self.assertEnglish(err.getvalue(), "purge stderr")

    def test_done_text_is_english(self):
        self.assertEnglish(i18n.t("purge_done"), "purge_done")

    def test_cli_error_goes_through_english(self):
        out, err = io.StringIO(), io.StringIO()
        with fake_store(paths.HOME), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--lang", "en", "switch", "9"])
        self.assertNotEqual(rc, 0)
        self.assertEnglish(out.getvalue() + err.getvalue(), "cli switch 9")


class DashboardHttpTests(_English):
    TOKEN = "english-output-token"

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

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": f"127.0.0.1:{self.port}", "X-LimitPilot-Token": self.TOKEN,
                   "X-LimitPilot": "1", "Origin": f"http://127.0.0.1:{self.port}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = resp.read().decode("utf-8")
        conn.close()
        return resp.status, payload

    def test_error_bodies_are_english(self):
        samples = [
            ("POST", "/api/slot/9/activate", {}),
            ("POST", "/api/slot/9/delete", {}),
            ("POST", "/api/settings", {}),
            ("POST", "/api/settings", {"keepalive": "maybe"}),
            ("POST", "/api/settings", {"autoSwitch": "maybe"}),
            ("POST", "/api/slot/9/alias", {"alias": 5}),
            ("POST", "/api/slot/9/refresh", {}),
            ("POST", "/api/add/code", {"code": "abc"}),
            ("POST", "/api/add/cancel", {}),
        ]
        seen_error = 0
        for method, path, body in samples:
            status, payload = self.request(method, path, body)
            self.assertEnglish(payload, f"{path} -> {status}")
            seen_error += status >= 400
        self.assertGreaterEqual(seen_error, 3)

    def test_internal_error_is_logged_in_english(self):
        err = io.StringIO()
        with (mock.patch.object(server.status_mod, "build_status", side_effect=RuntimeError("x")),
              contextlib.redirect_stderr(err)):
            status, payload = self.request("GET", "/api/status")
        self.assertEqual(status, 500)
        self.assertEnglish(payload + err.getvalue(), "/api/status failure")


if __name__ == "__main__":
    unittest.main()
