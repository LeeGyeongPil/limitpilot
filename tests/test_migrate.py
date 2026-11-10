"""AC-4: `limitpilot migrate` from claude-account-pilot — order, safety, dry-run, rollback.

This is one of the few files allowed to spell the previous tool's identifiers.
"""

import contextlib
import getpass
import io
import json
import os
import unittest
from unittest import mock

from limitpilot import legacy
from limitpilot import migrate
from limitpilot import paths
from limitpilot import state
from limitpilot.errors import KeychainError, LimitPilotError
from limitpilot.store import fake
from limitpilot.store import keychain
from tests.support import blob, fake_store, identity, temp_home

OLD_SERVICE = "claude-autoswap"


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self._home = temp_home()
        self.root = self._home.__enter__()
        self._store = fake_store(self.root)
        self._store.__enter__()
        self.timeline = []
        old = os.path.join(paths.HOME, ".claude-autoswap")
        self.old = old
        os.makedirs(os.path.join(old, "daemon"))
        with open(os.path.join(old, "daemon", "claude_autoswap.py"), "w") as handle:
            handle.write("# old daemon copy\n")
        plist = os.path.join(paths.HOME, "Library", "LaunchAgents",
                             f"com.{getpass.getuser()}.claude-autoswap.plist")
        os.makedirs(os.path.dirname(plist))
        with open(plist, "w") as handle:
            handle.write("<plist/>")
        self.plist = plist
        with open(os.path.join(old, "state.json"), "w") as handle:
            json.dump({"schemaVersion": 1, "activeSlot": 1, "lastNotify": {"x": 1},
                       "settings": {"threshold": 85, "keepalive": False},
                       "slots": {
                           "1": {"email": "user1@example.com", "oauthAccount": identity(1),
                                 "fetchBackoff": {"fails": 2}},
                           "2": {"email": "user2@example.com", "oauthAccount": identity(2)},
                           "3": {"kind": "gateway", "modelId": "example-model-id",
                                 "addedAt": "2026-01-01T00:00:00+00:00"},
                       }}, handle)
        fake.write(OLD_SERVICE, "slot-1", blob("old-access-1"))
        fake.write(OLD_SERVICE, "slot-2", blob("old-access-2"))
        fake.write(OLD_SERVICE, "slot-3", "gateway-secret-value")
        settings_path = paths.CLAUDE_SETTINGS_FILE
        with open(settings_path, "w") as handle:
            json.dump({"apiKeyHelper": f"/usr/bin/python3 {old}/daemon/claude_autoswap.py gateway-key 3",
                       "env": {"ANTHROPIC_MODEL": "example-model-id"}}, handle)
        os.makedirs(os.path.join(old, "oauth-login-abc"))
        # 시간 순서 기록: 하위 프로세스 호출과 저장소 쓰기·삭제
        real_write, real_delete = fake.write, fake.delete
        self.patches = [
            mock.patch("subprocess.run", side_effect=self._run),
            mock.patch.object(fake, "write", side_effect=lambda s, a, b: (
                self.timeline.append(("write", s, a)), real_write(s, a, b))[1]),
            mock.patch.object(fake, "delete", side_effect=lambda s, a=None: (
                self.timeline.append(("delete", s, a)), real_delete(s, a))[1]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._store.__exit__(None, None, None)
        self._home.__exit__(None, None, None)

    def _run(self, argv, **kwargs):
        self.timeline.append(("run", " ".join(argv[:2])))
        return mock.Mock(returncode=0, stdout="", stderr="")

    def _migrate(self, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return migrate.run(**kw)

    def test_stops_old_daemon_before_any_copy(self):
        self.assertEqual(self._migrate(), 0)
        kinds = [e[0] for e in migrate.EVENTS]
        self.assertLess(kinds.index("legacy-stop-marker"), kinds.index("copied"))
        self.assertLess(kinds.index("legacy-bootout"), kinds.index("copied"))
        bootout_at = self.timeline.index(("run", "launchctl bootout"))
        first_new_write = next(i for i, e in enumerate(self.timeline)
                               if e[0] == "write" and e[1] == paths.BACKUP_SERVICE)
        self.assertLess(bootout_at, first_new_write)
        self.assertTrue(os.path.exists(os.path.join(self.old, ".daemon-stopped")))
        self.assertFalse(os.path.exists(self.plist))
        self.assertFalse(os.path.exists(os.path.join(self.old, "daemon", "claude_autoswap.py")))

    def test_old_items_deleted_only_after_all_copies_verified(self):
        self._migrate()
        writes = [i for i, e in enumerate(self.timeline)
                  if e[0] == "write" and e[1] == paths.BACKUP_SERVICE]
        deletes = [i for i, e in enumerate(self.timeline)
                   if e[0] == "delete" and e[1] == OLD_SERVICE]
        self.assertEqual(len(writes), 3)
        self.assertEqual(len(deletes), 3)
        self.assertLess(max(writes), min(deletes))
        self.assertIn("old-access-1", fake.read(paths.BACKUP_SERVICE, "slot-1"))
        self.assertEqual(fake.read(paths.BACKUP_SERVICE, "slot-3"), "gateway-secret-value")
        self.assertIsNone(fake.read(OLD_SERVICE, "slot-1"))

    def test_state_settings_helper_and_marker(self):
        self._migrate()
        st = state.load_state()
        self.assertEqual(st["settings"]["threshold"], 85)
        self.assertFalse(st["settings"]["keepalive"])          # 사용자가 끈 값은 그대로
        self.assertTrue(st["settings"]["finalBurn"])           # 예전 기본값(켜짐) 유지
        self.assertTrue(st["settings"]["autoSwitch"])          # 데몬이 있었으므로 자동 전환 유지
        self.assertEqual(st["migratedFrom"]["tool"], "claude-account-pilot")
        self.assertEqual(st["lastNotify"], {})
        self.assertNotIn("fetchBackoff", st["slots"]["1"])
        with open(paths.CLAUDE_SETTINGS_FILE) as handle:
            helper = json.load(handle)["apiKeyHelper"]
        self.assertIn("limitpilot.pyz", helper)
        self.assertTrue(helper.endswith("gateway-key 3"))
        self.assertNotIn("claude_autoswap", helper)
        self.assertTrue(os.path.exists(os.path.join(self.old, "MIGRATED")))
        self.assertFalse(os.path.exists(os.path.join(self.old, "oauth-login-abc")))
        self.assertTrue(os.path.exists(os.path.join(self.old, "state.json")))   # 옛 폴더는 남긴다
        self.assertFalse(legacy.migration_pending())

    def test_failed_read_back_restores_new_side_and_keeps_old(self):
        real_read = keychain.kc_read

        def flaky(service, account):
            value = real_read(service, account)
            if service == paths.BACKUP_SERVICE and account == "slot-2":
                return "corrupted"
            return value

        with mock.patch.object(keychain, "kc_read", side_effect=flaky):
            with self.assertRaises(KeychainError):
                self._migrate()
        self.assertIsNone(fake.read(paths.BACKUP_SERVICE, "slot-1"))
        self.assertIsNone(fake.read(paths.BACKUP_SERVICE, "slot-2"))
        self.assertIn("old-access-1", fake.read(OLD_SERVICE, "slot-1"))
        self.assertIn("old-access-2", fake.read(OLD_SERVICE, "slot-2"))
        self.assertFalse(os.path.exists(paths.state_file()))
        self.assertFalse(os.path.exists(os.path.join(self.old, "MIGRATED")))

    def _snapshot(self):
        out = {}
        for base, _dirs, files in os.walk(self.root):
            for name in files:
                full = os.path.join(base, name)
                with open(full, "rb") as handle:
                    out[full] = handle.read()
        return out

    def test_dry_run_changes_nothing(self):
        before = self._snapshot()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(migrate.run(dry_run=True), 0)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.timeline, [])
        self.assertIn("1. stop the old daemon", out.getvalue())
        self.assertNotIn(getpass.getuser() + ".claude", out.getvalue())

    def test_refuses_to_overwrite_existing_limitpilot_state(self):
        os.makedirs(paths.STATE_DIR, exist_ok=True)
        state.save_state(state.default_state())
        with self.assertRaises(LimitPilotError):
            self._migrate()
        self.assertIn("old-access-1", fake.read(OLD_SERVICE, "slot-1"))

    def test_purge_legacy_only_after_migration(self):
        with self.assertRaises(LimitPilotError):
            self._migrate(purge_legacy=True)
        self._migrate()
        self._migrate(purge_legacy=True)
        self.assertFalse(os.path.exists(self.old))


if __name__ == "__main__":
    unittest.main()
