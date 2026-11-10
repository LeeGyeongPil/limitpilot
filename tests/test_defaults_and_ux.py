"""AC-7: public defaults are opt-in, quitting the UI keeps watching, deletes default to Cancel."""

import contextlib
import io
import json
import os
import re
import stat
import types
import unittest
from unittest import mock

from limitpilot import cli
from limitpilot import i18n
from limitpilot import notify as notify_mod
from limitpilot import paths
from limitpilot import state
from limitpilot import switch
from limitpilot import tick
from limitpilot.daemon import deploy
from limitpilot.store import filestore
from tests.support import (ROOT, FakeUsage, blob, fake_store, identity, temp_home, usage,
                           usage_source)

NATIVE = os.path.join(ROOT, "src", "limitpilot", "native")


def _read(*parts) -> str:
    with open(os.path.join(NATIVE, *parts), encoding="utf-8-sig") as handle:
        return handle.read()


class PublicDefaultTests(unittest.TestCase):
    def test_fresh_state_defaults(self):
        cfg = state.get_settings(state.default_state())
        self.assertFalse(cfg["keepalive"])
        self.assertFalse(cfg["finalBurn"])
        self.assertFalse(cfg["autoSwitch"])
        self.assertEqual(cfg["priority"], "expiring")
        self.assertEqual(cfg["threshold"], 90.0)

    def _over_threshold_world(self, store):
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
        return FakeUsage({"token-1": usage(session=95, weekly=20),
                          "token-2": usage(session=5, weekly=10)})

    def test_over_threshold_tick_only_notifies_when_auto_switch_is_off(self):
        with temp_home(), fake_store(paths.HOME) as store:
            src = self._over_threshold_world(store)
            with (
                usage_source(src),
                mock.patch.object(notify_mod, "notify") as notify,
                mock.patch.object(switch, "perform_switch") as perform,
                mock.patch("limitpilot.logutil.log"),
            ):
                for _ in range(3):
                    rc, _ = tick.run_tick()
                    self.assertEqual(rc, 2)
            perform.assert_not_called()                       # 전환 0회
            self.assertEqual(notify.call_count, 1)            # '전환했을 것' 알림 1회(중복 제거)
            title, body = notify.call_args.args
            self.assertEqual(title, i18n.t("notify_would_title"))
            self.assertIn("auto-switch on", body)               # 켜는 방법을 알려 준다
            self.assertIn("switch 2", body)
            self.assertEqual(state.load_state()["activeSlot"], 1)

    def test_same_tick_switches_when_auto_switch_is_on(self):
        with temp_home(), fake_store(paths.HOME) as store:
            src = self._over_threshold_world(store)
            st = state.load_state()
            st["settings"] = {"autoSwitch": True}
            state.save_state(st)
            with (
                usage_source(src),
                mock.patch.object(notify_mod, "notify"),
                mock.patch("limitpilot.logutil.log"),
            ):
                rc, _ = tick.run_tick()
            self.assertEqual(rc, 0)
            self.assertEqual(state.load_state()["activeSlot"], 2)

    def test_install_daemon_enable_auto_turns_auto_switch_on(self):
        with temp_home():
            completed = mock.Mock(returncode=0, stderr="", stdout="")
            with (
                mock.patch("subprocess.run", return_value=completed) as run,
                mock.patch.object(paths, "IS_WIN", False),
                mock.patch("limitpilot.logutil.log"),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                self.assertEqual(cli.cmd_install_daemon(types.SimpleNamespace(enable_auto=True)), 0)
            self.assertTrue(state.get_settings(state.load_state())["autoSwitch"])
            self.assertTrue(os.path.exists(deploy.deployed_daemon_script_path()))
            self.assertTrue(all(c.args[0][0] == "launchctl" for c in run.call_args_list))
            self.assertIn("switches automatically" if "switch" in out.getvalue() else "자동",
                          out.getvalue())

    def test_install_daemon_without_flag_is_watch_only(self):
        with temp_home():
            with (
                mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stderr="")),
                mock.patch.object(paths, "IS_WIN", False),
                mock.patch("limitpilot.logutil.log"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                cli.cmd_install_daemon(types.SimpleNamespace(enable_auto=False))
            self.assertFalse(state.get_settings(state.load_state())["autoSwitch"])

    def test_config_auto_switch_on_off(self):
        with temp_home(), mock.patch("limitpilot.logutil.log"):
            cli.cmd_config(types.SimpleNamespace(key="auto-switch", value="on"))
            self.assertTrue(state.get_settings(state.load_state())["autoSwitch"])
            cli.cmd_config(types.SimpleNamespace(key="auto-switch", value="off"))
            self.assertFalse(state.get_settings(state.load_state())["autoSwitch"])


class NativeUiTests(unittest.TestCase):
    def test_swift_quit_does_not_uninstall_the_daemon(self):
        src = _read("macos", "main.swift")
        self.assertNotIn("applicationWillTerminate", src)
        quit_body = re.search(r"@objc func quit\(\) \{(.*?)\}", src, re.S).group(1)
        self.assertNotIn("uninstall-daemon", quit_body)
        # 감시까지 끄는 것은 별도(Option 대체) 항목뿐
        self.assertIn("quitAndStopWatching", src)
        self.assertIn("isAlternate = true", src)
        self.assertNotIn("/bin/bash", src)                   # 셸 없이 인자 배열로 실행
        self.assertNotIn(".pyenv", src)

    def test_swift_delete_confirmation_defaults_to_cancel(self):
        src = _read("macos", "main.swift")
        self.assertIn("hasDestructiveAction", src)
        confirm = re.search(r"func confirm\(.*?\n    \}\n", src, re.S).group(0)
        self.assertLess(confirm.index('L("취소", "Cancel")'), confirm.index("addButton(withTitle: danger)"))
        self.assertIn(".alertSecondButtonReturn", confirm)

    def test_tray_quit_and_delete_defaults(self):
        cs = _read("windows", "tray.cs")
        self.assertNotIn("UninstallDaemonQuietly", cs)
        dispose = re.search(r"protected override void Dispose\(bool disposing\)(.*?)base\.Dispose", cs, re.S)
        self.assertNotIn("uninstall-daemon", dispose.group(1))
        self.assertIn("MessageBoxDefaultButton.Button2", cs)
        self.assertNotIn('MessageBoxButtons.YesNo, MessageBoxIcon.Warning);', cs)
        ps1 = _read("windows", "tray.ps1")
        self.assertIn('"YesNo", "Warning", "Button2"', ps1)
        self.assertNotIn("Uninstall-DaemonQuietly", ps1)

    def test_no_terracotta_or_old_names_in_native_sources(self):
        for rel in (("macos", "main.swift"), ("windows", "tray.cs"), ("windows", "tray.ps1"),
                    ("windows", "tray-start.cmd")):
            text = _read(*rel)
            for banned in ("217, 119, 87", "D97757", "Account Pilot", "AccountPilot"):
                self.assertNotIn(banned, text, rel)


class FileSafetyTests(unittest.TestCase):
    def test_atomic_write_keeps_existing_mode_and_new_files_are_private(self):
        with temp_home():
            target = paths.CLAUDE_JSON
            with open(target, "w") as handle:
                handle.write("{}")
            os.chmod(target, 0o644)
            filestore.atomic_write_json(target, {"a": 1})
            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o644)
            fresh = os.path.join(os.path.dirname(target), "new.json")
            filestore.atomic_write_json(fresh, {"b": 2})
            self.assertEqual(stat.S_IMODE(os.stat(fresh).st_mode), 0o600)
            leftovers = [n for n in os.listdir(os.path.dirname(target)) if n.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_login_buffer_is_capped(self):
        from limitpilot.login import session

        self.assertEqual(session.LOGIN_BUFFER_MAX, 64 * 1024)
        s = session.ClaudeLoginSession.__new__(session.ClaudeLoginSession)
        s.buf = ""
        s._append("x" * (200 * 1024))
        s._append("tail-marker")
        self.assertLessEqual(len(s.buf), 64 * 1024)
        self.assertTrue(s.buf.endswith("tail-marker"))


if __name__ == "__main__":
    unittest.main()
