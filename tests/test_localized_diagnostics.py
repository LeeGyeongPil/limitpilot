"""doctor, ``migrate --dry-run`` and ``widget --dry-run`` follow ``--lang`` (they used to be English only).

Rules checked here: Korean output has Hangul on every line that carries a description; the
``ok``/``warn``/``fail`` markers, paths, numbers and commands stay as they are; English output has no
Hangul. The exact English wording is pinned by the older tests (``test_migrate``, ``test_packaging``).
"""

import contextlib
import io
import json
import os
import re
import types
import unittest
from unittest import mock

from limitpilot import doctor
from limitpilot import i18n
from limitpilot import migrate
from limitpilot import paths
from limitpilot import widget
from tests.support import fake_store, temp_home

HANGUL = re.compile("[ᄀ-ᇿ㄰-㆏가-힣]")


def _capture(fn, *args, **kwargs) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn(*args, **kwargs)
    return out.getvalue()


class _LangCase(unittest.TestCase):
    def use_lang(self, lang: str) -> None:
        # set_lang 은 환경변수도 바꾼다 — 둘 다 되돌려야 다른 테스트(기본 ko)에 새지 않는다
        previous = i18n._override
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(lambda: setattr(i18n, "_override", previous))
        i18n.set_lang(lang)


class DoctorLanguageTests(_LangCase):
    def setUp(self):
        home = temp_home()
        self.root = home.__enter__()
        self.addCleanup(home.__exit__, None, None, None)
        store = fake_store(self.root)
        store.__enter__()
        self.addCleanup(store.__exit__, None, None, None)
        port = mock.patch("socket.socket.connect_ex", return_value=1)   # 포트는 비어 있다고 가정
        port.start()
        self.addCleanup(port.stop)

    def _claude_json(self, body: str) -> None:
        with open(paths.CLAUDE_JSON, "w", encoding="utf-8") as handle:
            handle.write(body)

    def test_ko_every_line_has_hangul_and_keeps_marker(self):
        self._claude_json(json.dumps({"oauthAccount": {"accountUuid": "u", "emailAddress": "e"}}))
        self.use_lang("ko")
        lines = _capture(doctor.run).splitlines()
        self.assertGreaterEqual(len(lines), 5)
        for line in lines:
            self.assertRegex(line, r"^(ok|warn|fail)\s", line)
            self.assertRegex(line, HANGUL, line)
        text = "\n".join(lines)
        for english in ("oauthAccount found", "daemon not installed", "dashboard port", "credential store",
                        "activity log", "turns on watching"):
            self.assertNotIn(english, text)
        self.assertIn("8748", text)            # 숫자·값은 그대로
        self.assertIn("install-daemon", text)  # 명령어는 그대로

    def test_ko_warning_and_failure_branches(self):
        self.use_lang("ko")
        for body in (None, "{}", "{broken"):
            if body is not None:
                self._claude_json(body)
            for line in _capture(doctor.run).splitlines():
                self.assertRegex(line, HANGUL, line)
        with mock.patch.object(doctor.deploy, "deployed_daemon_script_path", return_value=__file__), \
                mock.patch.object(doctor.deploy, "script_app_version", return_value=(0, 0, 1)):
            self.assertRegex(_capture(doctor.run), r"warn .*[가-힣]")
        with mock.patch.object(doctor.deploy, "deployed_daemon_script_path", return_value=__file__), \
                mock.patch.object(doctor.deploy, "script_app_version", return_value=None):
            for line in _capture(doctor.run).splitlines():
                self.assertRegex(line, HANGUL, line)
        with mock.patch.object(doctor.legacy, "running_legacy_daemons", return_value=["/x/trace"]), \
                mock.patch.object(doctor.sys, "platform", "linux"), \
                mock.patch.object(doctor.sys, "version_info", (3, 9, 0)), \
                mock.patch("socket.socket.connect_ex", return_value=0):
            lines = _capture(doctor.run).splitlines()
        for line in lines:
            self.assertRegex(line, HANGUL, line)
        self.assertTrue(any(line.startswith("fail") for line in lines))
        self.assertTrue(any(line.startswith("warn") for line in lines))

    def test_en_has_no_hangul(self):
        self._claude_json(json.dumps({"oauthAccount": {"accountUuid": "u", "emailAddress": "e"}}))
        self.use_lang("en")
        text = _capture(doctor.run)
        self.assertNotRegex(text, HANGUL)
        self.assertIn("ok    ~/.claude.json — oauthAccount found", text)
        self.assertIn("ok    dashboard port 8748 — free", text)


class MigrateDryRunLanguageTests(_LangCase):
    def setUp(self):
        home = temp_home()
        self.root = home.__enter__()
        self.addCleanup(home.__exit__, None, None, None)
        old = os.path.join(paths.HOME, ".claude-autoswap")
        os.makedirs(old)
        with open(os.path.join(old, "state.json"), "w", encoding="utf-8") as handle:
            json.dump({"schemaVersion": 1, "slots": {"1": {"email": "a@example.com"},
                                                      "2": {"email": "b@example.com"}}}, handle)

    def _with_new_state(self) -> None:
        os.makedirs(paths.STATE_DIR, exist_ok=True)
        with open(paths.state_file(), "w", encoding="utf-8") as handle:
            handle.write("{}")

    def test_ko_header_plan_and_note_have_hangul(self):
        self._with_new_state()
        self.use_lang("ko")
        lines = _capture(migrate.run, dry_run=True).splitlines()
        self.assertEqual(len(lines), 1 + 7 + 1)   # 머리말 + 계획 7단계 + note
        for line in lines:
            self.assertRegex(line, HANGUL, line)
        for number in range(1, 8):
            self.assertTrue(any(line.strip().startswith(f"{number}.") for line in lines), number)
        text = "\n".join(lines)
        self.assertIn("[1, 2]", text)                       # 슬롯 목록은 그대로
        self.assertIn(paths.state_file(), text)             # 경로는 그대로
        self.assertNotIn("stop the old daemon", text)
        self.assertNotIn("already exists", text)

    def test_ko_without_new_state_has_no_note(self):
        self.use_lang("ko")
        lines = _capture(migrate.run, dry_run=True).splitlines()
        self.assertEqual(len(lines), 8)
        for line in lines:
            self.assertRegex(line, HANGUL, line)

    def test_en_has_no_hangul(self):
        self._with_new_state()
        self.use_lang("en")
        text = _capture(migrate.run, dry_run=True)
        self.assertNotRegex(text, HANGUL)
        self.assertIn("migrate --dry-run (nothing will be changed):", text)
        self.assertIn("  4. copy 2 slot credential(s) [1, 2] and read each back; then delete the old items", text)
        self.assertIn("already exists — a real run stops before step 1", text)


class WidgetDryRunLanguageTests(_LangCase):
    def test_ko_macos_windows_and_remove_have_hangul(self):
        self.use_lang("ko")
        for text in (
            _capture(widget.build_macos, "/tmp/x", dry_run=True),
            _capture(widget.build_windows, "/tmp/x", dry_run=True),
            _capture(widget.run, types.SimpleNamespace(action="remove", out="/tmp/x", dry_run=True)),
        ):
            lines = text.splitlines()
            self.assertTrue(lines)
            for line in lines:
                self.assertRegex(line, HANGUL, line)
            self.assertNotIn("would ", text)
        mac = _capture(widget.build_macos, "/tmp/x", dry_run=True)
        self.assertIn("swiftc -O main.swift", mac)         # 명령어·경로는 그대로
        self.assertIn("io.github.limitpilot", mac)

    def test_en_has_no_hangul_and_same_wording(self):
        self.use_lang("en")
        mac = _capture(widget.build_macos, "/tmp/x", dry_run=True)
        self.assertNotRegex(mac, HANGUL)
        self.assertTrue(mac.startswith("would build: swiftc -O main.swift"))
        remove = _capture(widget.run, types.SimpleNamespace(action="remove", out="/tmp/x", dry_run=True))
        self.assertTrue(remove.startswith("would remove: "))
        self.assertNotRegex(remove, HANGUL)


if __name__ == "__main__":
    unittest.main()
