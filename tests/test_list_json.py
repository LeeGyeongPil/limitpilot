"""`list --json` / `status --json`: stable machine-readable output (schemaVersion 1)."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from limitpilot import cli
from limitpilot import collector
from limitpilot import i18n
from limitpilot import state
from limitpilot.errors import LimitPilotError

from tests.support import ROOT, SRC, temp_home

TOP_KEYS = {"schemaVersion", "version", "activeSlot", "slots"}
SLOT_KEYS = {"slot", "label", "active", "enabled", "kind", "status", "fiveHour", "sevenDay"}


def _run(tmp, *argv):
    """Run the CLI in a subprocess in practice mode; returns CompletedProcess."""
    env = dict(os.environ, PYTHONPATH=SRC, PYTHONDONTWRITEBYTECODE="1",
               LIMITPILOT_DEMO_ROOT=os.path.join(tmp, "limitpilot-demo"))
    for key in ("LIMITPILOT_HOME", "LIMITPILOT_CLAUDE_HOME", "LIMITPILOT_LANG", "LANG",
                "LC_ALL", "LC_MESSAGES"):
        env.pop(key, None)
    return subprocess.run([sys.executable, "-B", "-m", "limitpilot", "--demo", *argv],
                          capture_output=True, text=True, env=env, timeout=120, cwd=ROOT)


class ListJsonDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = cls._tmp.name
        cls.list_en = _run(tmp, "--lang", "en", "list", "--json")
        cls.list_ko = _run(tmp, "--lang", "ko", "list", "--json")
        cls.status_en = _run(tmp, "--lang", "en", "status", "--json")
        cls.human = _run(tmp, "--lang", "en", "list")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_list_and_status_parse_with_same_structure(self):
        for res in (self.list_en, self.status_en):
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue(res.stdout.endswith("}\n"))
            self.assertEqual(res.stdout.count("\n"), 1)
        a, b = json.loads(self.list_en.stdout), json.loads(self.status_en.stdout)
        self.assertEqual(a, b)

    def test_required_keys_and_schema_version(self):
        doc = json.loads(self.list_en.stdout)
        self.assertEqual(set(doc), TOP_KEYS)
        self.assertEqual(doc["schemaVersion"], 1)
        self.assertEqual(doc["activeSlot"], 1)
        self.assertEqual(len(doc["slots"]), 3)
        for slot in doc["slots"]:
            self.assertEqual(set(slot), SLOT_KEYS)
        first = doc["slots"][0]
        self.assertTrue(first["active"])
        self.assertEqual(first["status"], "ok")
        self.assertEqual(set(first["fiveHour"]), {"pct", "resetsAt"})
        self.assertIsInstance(first["fiveHour"]["pct"], float)
        self.assertIsInstance(first["sevenDay"]["resetsAt"], str)
        self.assertEqual(doc["slots"][2]["status"], "quarantined")
        self.assertIsNone(doc["slots"][2]["fiveHour"])

    def test_no_tokens_or_free_text(self):
        out = self.list_en.stdout.lower()
        for needle in ("demo-access", "demo-refresh", "token", "invalid_grant", "baseurl"):
            self.assertNotIn(needle, out)

    def test_language_does_not_change_json(self):
        self.assertEqual(self.list_ko.returncode, 0, self.list_ko.stderr)
        self.assertEqual(self.list_en.stdout, self.list_ko.stdout)

    def test_human_output_has_no_json_and_still_works(self):
        self.assertEqual(self.human.returncode, 0, self.human.stderr)
        self.assertIn("Active slot: 1", self.human.stdout)
        self.assertNotIn("schemaVersion", self.human.stdout)

    def test_json_flag_in_help_for_list_and_status(self):
        for cmd in ("list", "status"):
            res = subprocess.run([sys.executable, "-B", "-m", "limitpilot", "--lang", "en",
                                  cmd, "--help"], capture_output=True, text=True, timeout=60,
                                 env=dict(os.environ, PYTHONPATH=SRC), cwd=ROOT)
            self.assertIn("--json", res.stdout)


class ListJsonUnitTests(unittest.TestCase):
    def setUp(self):
        # cli.main(--lang ...) changes process-wide language state; put it back afterwards
        saved_override = i18n._override
        saved_env = os.environ.get("LIMITPILOT_LANG")

        def restore():
            i18n._override = saved_override
            if saved_env is None:
                os.environ.pop("LIMITPILOT_LANG", None)
            else:
                os.environ["LIMITPILOT_LANG"] = saved_env
        self.addCleanup(restore)

    def _main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_no_slots_is_not_an_error(self):
        with temp_home(), mock.patch.object(cli.deploy, "maybe_refresh_daemon_copy"), \
                mock.patch.object(sys, "platform", "darwin"):
            rc, out, _ = self._main("list", "--json")
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertEqual(doc["slots"], [])
        self.assertIsNone(doc["activeSlot"])

    def test_error_path_prints_error_json_and_keeps_exit_code(self):
        boom = LimitPilotError("disk is full")
        with mock.patch.object(cli.deploy, "maybe_refresh_daemon_copy"), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(collector, "collect_and_merge", side_effect=boom):
            rc, out, err = self._main("--lang", "en", "status", "--json")
        self.assertEqual(rc, 1)
        doc = json.loads(out)
        self.assertEqual(doc["schemaVersion"], 1)
        self.assertEqual(doc["error"]["code"], "error")
        self.assertIn("disk is full", doc["error"]["message"])
        self.assertIn("disk is full", err)

    def test_unexpected_error_code(self):
        with mock.patch.object(cli.deploy, "maybe_refresh_daemon_copy"), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(collector, "collect_and_merge", side_effect=ValueError("x")):
            rc, out, _ = self._main("--lang", "en", "list", "--json")
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "unexpected")

    def test_argument_error_stays_exit_2(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            cli.main(["switch", "--json"])
        self.assertEqual(ctx.exception.code, 2)

    def test_status_codes_mirror_slot_detail_branches(self):
        self.assertEqual(cli._slot_status({"kind": "setup-token"}, None), "unknown")
        self.assertEqual(cli._slot_status({"quarantine": {"reason": "r"}}, None), "quarantined")
        self.assertEqual(cli._slot_status({}, cli.TOKEN_EXPIRED), "token-expired")
        self.assertEqual(cli._slot_status({}, {"five_hour": {"pct": 1}}), "partial")
        self.assertEqual(cli._slot_status({"usage": {"five_hour": {"pct": 1},
                                                      "seven_day": {"pct": 2}}}, None), "snapshot")
        self.assertEqual(cli._slot_status({}, None), "unknown")
        self.assertEqual(state.slot_label({"alias": "a"}, 1), "a")


if __name__ == "__main__":
    unittest.main()
