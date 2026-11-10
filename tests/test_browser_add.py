import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unicodedata
import unittest
from contextlib import nullcontext
from unittest import mock

from limitpilot import accounts
from limitpilot import cli as cli_mod
from limitpilot import collector
from limitpilot import constants
from limitpilot import errors
from limitpilot import locks
from limitpilot import logutil
from limitpilot import oauth as oauth_mod
from limitpilot import paths
from limitpilot import state as state_mod
from limitpilot import switch
from limitpilot import usage_api
from limitpilot.dashboard import server
from limitpilot.dashboard import status as status_mod
from limitpilot.login import session as session_mod
from limitpilot.policy import decide
from limitpilot.store import creds
from limitpilot.store import filestore
from limitpilot.store import keychain


FULL_SCOPES = [
    "org:create_api_key",
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
]


def oauth_blob(**overrides):
    oauth = {
        "accessToken": "test-access-token",
        "refreshToken": "test-refresh-token",
        "expiresAt": 4_102_444_800_000,
        "scopes": list(FULL_SCOPES),
        "subscriptionType": "max",
    }
    oauth.update(overrides)
    return json.dumps({"claudeAiOauth": oauth})


def oauth_account(**overrides):
    account = {
        "emailAddress": "person@example.com",
        "accountUuid": "account-uuid-1",
        "organizationUuid": "organization-uuid-1",
        "organizationName": "Example Org",
        "displayName": "Test Person",
    }
    account.update(overrides)
    return account


class IsolatedOAuthCredentialsTests(unittest.TestCase):
    def test_keychain_service_hashes_exact_nfc_config_path(self):
        composed = "/tmp/claude-caf\u00e9"
        decomposed = unicodedata.normalize("NFD", composed)
        expected_hash = hashlib.sha256(
            unicodedata.normalize("NFC", composed).encode("utf-8")
        ).hexdigest()[:8]

        self.assertEqual(
            session_mod.isolated_keychain_service(composed),
            f"Claude Code-credentials-{expected_hash}",
        )
        self.assertEqual(
            session_mod.isolated_keychain_service(decomposed),
            session_mod.isolated_keychain_service(composed),
        )
        self.assertNotEqual(
            session_mod.isolated_keychain_service(composed + "/"),
            session_mod.isolated_keychain_service(composed),
        )

    def test_loads_credentials_from_isolated_hashed_keychain(self):
        blob = oauth_blob()
        account = oauth_account()
        with tempfile.TemporaryDirectory() as config_dir:
            with open(os.path.join(config_dir, ".claude.json"), "w", encoding="utf-8") as f:
                json.dump({"oauthAccount": account, "unrelated": True}, f)
            with mock.patch.object(keychain, "kc_read", return_value=blob) as read:
                loaded_blob, loaded_account = session_mod.load_isolated_login(config_dir)

        self.assertEqual(loaded_blob, blob)
        self.assertEqual(loaded_account, account)
        read.assert_called_with(session_mod.isolated_keychain_service(config_dir), paths.KC_USER)

    def test_falls_back_to_isolated_credentials_file(self):
        blob = oauth_blob()
        account = oauth_account()
        with tempfile.TemporaryDirectory() as config_dir:
            with open(
                os.path.join(config_dir, ".credentials.json"), "w", encoding="utf-8"
            ) as f:
                f.write(blob)
            with open(os.path.join(config_dir, ".claude.json"), "w", encoding="utf-8") as f:
                json.dump({"oauthAccount": account}, f)
            with mock.patch.object(keychain, "kc_read", return_value=None):
                loaded_blob, loaded_account = session_mod.load_isolated_login(config_dir)

        self.assertEqual(loaded_blob, blob)
        self.assertEqual(loaded_account, account)

    def test_cleanup_deletes_only_hashed_service_and_profile(self):
        with tempfile.TemporaryDirectory() as state_dir:
            config_dir = os.path.join(state_dir, "oauth-login-test-profile")
            service = session_mod.isolated_keychain_service(config_dir)
            with (
                mock.patch.object(paths, "STATE_DIR", state_dir),
                mock.patch.object(subprocess, "run") as run,
                mock.patch.object(shutil, "rmtree") as rmtree,
            ):
                run.return_value.returncode = 0
                session_mod.cleanup_isolated_auth(config_dir)

        command = run.call_args.args[0]
        self.assertEqual(
            command,
            [
                paths.SECURITY,
                "delete-generic-password",
                "-a",
                paths.KC_USER,
                "-s",
                service,
            ],
        )
        self.assertRegex(command[-1], r"^Claude Code-credentials-[0-9a-f]{8}$")
        self.assertNotEqual(command[-1], paths.ACTIVE_SERVICE)
        rmtree.assert_called_once_with(os.path.realpath(config_dir), ignore_errors=True)

    def test_cleanup_refuses_unhashed_default_service(self):
        with (
            mock.patch.object(
                session_mod, "isolated_keychain_service", return_value=paths.ACTIVE_SERVICE
            ),
            mock.patch.object(subprocess, "run") as run,
        ):
            with self.assertRaises(errors.LimitPilotError):
                session_mod.cleanup_isolated_auth("/tmp/unsafe-profile")

        run.assert_not_called()

    def test_cleanup_retries_and_warns_when_keychain_delete_fails(self):
        with tempfile.TemporaryDirectory() as state_dir:
            config_dir = os.path.join(state_dir, "oauth-login-test-profile")
            with (
                mock.patch.object(paths, "STATE_DIR", state_dir),
                mock.patch.object(subprocess, "run") as run,
                mock.patch.object(shutil, "rmtree") as rmtree,
                mock.patch.object(time, "sleep"),
                mock.patch.object(logutil, "log") as log,
            ):
                run.return_value.returncode = 1
                session_mod.cleanup_isolated_auth(config_dir)

        self.assertEqual(run.call_count, 3)
        self.assertIn("Keychain 정리 실패", log.call_args.args[0])
        self.assertIn(session_mod.isolated_keychain_service(config_dir), log.call_args.args[0])
        rmtree.assert_not_called()  # service hash 매핑을 남겨 cleanup/purge에서 재시도


class OAuthValidationTests(unittest.TestCase):
    def test_accepts_refreshable_full_scope_credentials_and_identity(self):
        session_mod.validate_login_credentials(oauth_blob(), oauth_account())

    def test_rejects_missing_refresh_token(self):
        with self.assertRaises(errors.LimitPilotError):
            session_mod.validate_login_credentials(oauth_blob(refreshToken=""), oauth_account())

    def test_rejects_every_missing_required_scope(self):
        for missing in constants.LOGIN_REQUIRED_SCOPES:
            with self.subTest(missing=missing):
                scopes = [scope for scope in FULL_SCOPES if scope != missing]
                with self.assertRaises(errors.LimitPilotError):
                    session_mod.validate_login_credentials(
                        oauth_blob(scopes=scopes), oauth_account()
                    )

    def test_rejects_non_numeric_expiry_and_incomplete_identity(self):
        with self.assertRaises(errors.LimitPilotError):
            session_mod.validate_login_credentials(
                oauth_blob(expiresAt="4102444800000"), oauth_account()
            )
        with self.assertRaises(errors.LimitPilotError):
            session_mod.validate_login_credentials(
                oauth_blob(), oauth_account(emailAddress="")
            )


def _pty_available() -> bool:
    """Some sandboxes (e.g. locked-down CI runners) cannot open pseudo-terminals."""
    if sys.platform == "win32":
        return False
    try:
        master, slave = os.openpty()
    except OSError:
        return False
    os.close(master)
    os.close(slave)
    return True


_PTY_OK = _pty_available()


class ClaudeLoginSessionTests(unittest.TestCase):
    SENSITIVE_ENV = (
        "CLAUDE_SECURESTORAGE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
        "CLAUDE_CODE_OAUTH_SCOPES",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_CUSTOM_OAUTH_URL",
        "CLAUDE_CODE_OAUTH_CLIENT_ID",
    )

    @unittest.skipUnless(_PTY_OK, "pseudo-terminals are not available in this environment")
    def test_uses_official_login_in_scrubbed_isolated_process(self):
        blob = oauth_blob()
        account = oauth_account()
        with tempfile.TemporaryDirectory() as tmp:
            cli = os.path.join(tmp, "fake-claude")
            capture = os.path.join(tmp, "capture.json")
            script = textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import json
                import os
                import sys

                sensitive = {self.SENSITIVE_ENV!r}
                with open(os.environ["TEST_ENV_CAPTURE"], "w", encoding="utf-8") as f:
                    json.dump({{
                        "argv": sys.argv[1:],
                        "configDir": os.environ.get("CLAUDE_CONFIG_DIR"),
                        "sensitivePresent": [key for key in sensitive if key in os.environ],
                        "newSession": os.getsid(0) == os.getpid(),
                    }}, f)
                print("https://claude.ai/oauth/authorize?state=test", flush=True)
                print("Paste code here if prompted > ", end="", flush=True)
                if sys.stdin.readline().strip() != "auth-code#state":
                    raise SystemExit(2)
                print("Login successful", flush=True)
                """
            )
            with open(cli, "w", encoding="utf-8") as f:
                f.write(script)
            os.chmod(cli, 0o700)

            inherited = {key: "must-not-leak" for key in self.SENSITIVE_ENV}
            inherited.update(
                {
                    "CLAUDE_CONFIG_DIR": "/tmp/default-profile-must-not-be-used",
                    "TEST_ENV_CAPTURE": capture,
                }
            )
            with (
                mock.patch.dict(os.environ, inherited),
                mock.patch.object(paths, "STATE_DIR", tmp),
                mock.patch.object(session_mod, "find_claude_bin", return_value=cli),
                mock.patch.object(
                    session_mod, "load_isolated_login", return_value=(blob, account)
                ) as load,
                mock.patch.object(session_mod, "cleanup_isolated_auth") as cleanup,
            ):
                session = session_mod.ClaudeLoginSession()
                config_dir = session.config_dir
                info = session.start(timeout=2)
                self.assertIn("oauth/authorize", info["url"])
                session.feed("auth-code#state")
                self.assertEqual(
                    session.wait_for_credentials(timeout=3), (blob, account)
                )

            with open(capture, encoding="utf-8") as f:
                invocation = json.load(f)
            self.assertEqual(invocation["argv"], ["auth", "login", "--claudeai"])
            self.assertEqual(invocation["configDir"], config_dir)
            self.assertTrue(os.path.isabs(config_dir))
            self.assertEqual(invocation["sensitivePresent"], [])
            self.assertTrue(invocation["newSession"])
            load.assert_called_once_with(config_dir)
            cleanup.assert_called_once_with(config_dir)
            self.assertIsNone(session.master)
            self.assertIsNone(session.proc)

    @unittest.skipUnless(_PTY_OK, "pseudo-terminals are not available in this environment")
    def test_close_terminates_process_group_and_cleans_isolated_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = os.path.join(tmp, "fake-claude")
            script = textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import time
                print("https://claude.ai/oauth/authorize?state=test", flush=True)
                time.sleep(60)
                """
            )
            with open(cli, "w", encoding="utf-8") as f:
                f.write(script)
            os.chmod(cli, 0o700)

            with (
                mock.patch.object(paths, "STATE_DIR", tmp),
                mock.patch.object(session_mod, "find_claude_bin", return_value=cli),
                mock.patch.object(session_mod, "cleanup_isolated_auth") as cleanup,
            ):
                session = session_mod.ClaudeLoginSession()
                config_dir = session.config_dir
                session.start(timeout=2)
                proc = session.proc
                session.close()
                session.close()

            self.assertIsNotNone(proc.returncode)
            self.assertIsNone(session.master)
            self.assertIsNone(session.proc)
            cleanup.assert_called_once_with(config_dir)

    def test_debug_log_contains_only_stage_flags_and_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = "auth-code-that-must-not-be-written"
            output = (
                "https://claude.ai/oauth/authorize?code=" + secret
                + " Paste code here if prompted > Login successful"
            )
            with mock.patch.object(paths, "STATE_DIR", tmp):
                session_mod._dump_add_debug(output, exit_code=0)
            path = os.path.join(tmp, "add-debug.log")
            with open(path, encoding="utf-8") as f:
                diagnostic = json.load(f)

            serialized = json.dumps(diagnostic)
            self.assertTrue(diagnostic["authorizeUrlSeen"])
            self.assertTrue(diagnostic["codePromptSeen"])
            self.assertTrue(diagnostic["loginSuccessfulSeen"])
            self.assertNotIn(secret, serialized)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class OAuthRegistrationTests(unittest.TestCase):
    @staticmethod
    def existing_slot(account=None, **overrides):
        account = account or oauth_account()
        slot = {
            "alias": None,
            "email": account["emailAddress"],
            "orgName": account.get("organizationName"),
            "oauthAccount": account,
            "usage": None,
            "usageFetchedAt": None,
            "quarantine": None,
            "enabled": True,
            "kind": "oauth",
        }
        slot.update(overrides)
        return slot

    def test_registers_real_identity_without_changing_active_slot(self):
        blob = oauth_blob()
        account = oauth_account()
        state = state_mod.default_state()
        state["slots"]["1"] = self.existing_slot(
            oauth_account(
                emailAddress="active@example.com",
                accountUuid="active-account",
                organizationUuid="active-org",
            )
        )
        state["activeSlot"] = 1

        with (
            mock.patch.object(creds, "kc_write_slot") as write,
            mock.patch.object(state_mod, "save_state") as save,
        ):
            result = accounts.register_oauth_slot(state, blob, account, alias="work")

        self.assertEqual(result, (2, "work", True))
        self.assertEqual(state["activeSlot"], 1)
        slot = state["slots"]["2"]
        self.assertEqual(slot["email"], "person@example.com")
        self.assertEqual(slot["oauthAccount"], account)
        self.assertEqual(slot["orgName"], "Example Org")
        self.assertEqual(slot["kind"], "oauth")
        self.assertTrue(slot["enabled"])
        write.assert_called_once_with(2, blob)
        save.assert_called_once_with(state)

    def test_identity_uuid_updates_inactive_slot_even_if_email_changed(self):
        old_account = oauth_account(emailAddress="old@example.com")
        new_account = oauth_account(emailAddress="new@example.com")
        state = state_mod.default_state()
        state["slots"]["4"] = self.existing_slot(old_account, alias="kept")

        with (
            mock.patch.object(creds, "kc_write_slot"),
            mock.patch.object(state_mod, "save_state"),
        ):
            result = accounts.register_oauth_slot(state, oauth_blob(), new_account)

        self.assertEqual(result, (4, "kept", False))
        self.assertEqual(state["slots"]["4"]["email"], "new@example.com")
        self.assertEqual(state["slots"]["4"]["oauthAccount"], new_account)

    def test_same_email_with_different_modern_uuid_creates_new_slot(self):
        old_account = oauth_account(accountUuid="old-account")
        new_account = oauth_account(accountUuid="new-account")
        state = state_mod.default_state()
        state["slots"]["1"] = self.existing_slot(old_account)

        with (
            mock.patch.object(creds, "kc_write_slot"),
            mock.patch.object(state_mod, "save_state"),
        ):
            result = accounts.register_oauth_slot(state, oauth_blob(), new_account)

        self.assertEqual(result, (2, "person@example.com", True))
        self.assertEqual(state["slots"]["1"]["oauthAccount"], old_account)
        self.assertEqual(state["slots"]["2"]["oauthAccount"], new_account)

    def test_cannot_overwrite_same_identity_when_its_slot_is_active(self):
        state = state_mod.default_state()
        state["slots"]["3"] = self.existing_slot()
        state["activeSlot"] = 3

        with (
            mock.patch.object(creds, "kc_write_slot") as write,
            mock.patch.object(state_mod, "save_state"),
        ):
            with self.assertRaisesRegex(errors.LimitPilotError, "활성 슬롯"):
                accounts.register_oauth_slot(state, oauth_blob(), oauth_account())

        write.assert_not_called()
        self.assertEqual(state["activeSlot"], 3)

    def test_snapshot_add_rejects_inference_only_credentials(self):
        limited = json.dumps({"claudeAiOauth": {
            "accessToken": "limited-access-token",
            "expiresAt": 4_102_444_800_000,
            "scopes": ["user:inference"],
        }})
        with (
            mock.patch.object(locks, "state_lock", return_value=nullcontext()),
            mock.patch.object(state_mod, "load_state", return_value=state_mod.default_state()),
            mock.patch.object(creds, "kc_read_active", return_value=limited),
            mock.patch.object(filestore, "read_json", return_value={
                "oauthAccount": oauth_account()
            }),
        ):
            with self.assertRaises(errors.LimitPilotError):
                cli_mod.cmd_add(mock.Mock())

    def test_switch_backs_up_live_credentials_by_full_identity(self):
        email = "shared@example.com"
        first = oauth_account(
            emailAddress=email, accountUuid="account-1", organizationUuid="org-1"
        )
        second = oauth_account(
            emailAddress=email, accountUuid="account-2", organizationUuid="org-2"
        )
        target = oauth_account(
            emailAddress="target@example.com",
            accountUuid="account-3",
            organizationUuid="org-3",
        )
        state = state_mod.default_state()
        state["slots"] = {
            "1": self.existing_slot(first),
            "2": self.existing_slot(second),
            "3": self.existing_slot(target),
        }
        state["activeSlot"] = 2
        live_blob = "live-credentials"
        target_blob = "target-credentials"

        with (
            mock.patch.object(locks, "proper_lockfile", return_value=nullcontext()),
            mock.patch.object(creds, "kc_read_active", side_effect=[live_blob, target_blob]),
            mock.patch.object(creds, "kc_read_slot", return_value=target_blob),
            mock.patch.object(creds, "kc_write_slot") as write_slot,
            mock.patch.object(creds, "kc_write_active"),
            mock.patch.object(filestore, "read_json", return_value={"oauthAccount": second}),
            mock.patch.object(switch, "splice_oauth_account"),
            mock.patch.object(switch, "bump_creds_mtime"),
            mock.patch.object(state_mod, "save_state"),
        ):
            switch.perform_switch(state, 3)

        write_slot.assert_called_once_with(2, live_blob)

    def test_registration_marks_matching_default_live_account_active(self):
        account = oauth_account(accountUuid="new-live-account")
        other = oauth_account(
            emailAddress="other@example.com",
            accountUuid="other-account",
            organizationUuid="other-org",
        )
        state = state_mod.default_state()
        state["slots"]["1"] = self.existing_slot(other)
        state["activeSlot"] = 1

        with (
            mock.patch.object(accounts, "read_live_oauth_account", return_value=account),
            mock.patch.object(creds, "kc_write_slot") as write_slot,
            mock.patch.object(creds, "kc_write_active") as write_active,
            mock.patch.object(state_mod, "save_state") as save,
        ):
            result = accounts.register_oauth_slot(state, oauth_blob(), account)

        self.assertEqual(result[0], 2)
        self.assertEqual(state["activeSlot"], 2)
        write_slot.assert_called_once_with(2, oauth_blob())
        write_active.assert_not_called()
        save.assert_called_once_with(state)


class LiveAccountReconciliationTests(unittest.TestCase):
    @staticmethod
    def slot(account, **overrides):
        value = {
            "email": account["emailAddress"],
            "oauthAccount": account,
            "enabled": True,
            "kind": "oauth",
        }
        value.update(overrides)
        return value

    def test_inspection_uses_uuid_not_same_email(self):
        registered = oauth_account(
            emailAddress="shared@example.com",
            accountUuid="registered-account",
            organizationUuid="registered-org",
        )
        live = oauth_account(
            emailAddress="shared@example.com",
            accountUuid="different-account",
            organizationUuid="different-org",
        )
        state = state_mod.default_state()
        state["activeSlot"] = 1
        state["slots"]["1"] = self.slot(registered)

        with mock.patch.object(accounts, "read_live_oauth_account", return_value=live):
            binding = accounts.inspect_live_account(state)

        self.assertEqual(binding["status"], "unmanaged")
        self.assertIsNone(binding["slot"])
        self.assertNotIn("oauthAccount", binding)
        self.assertEqual(state["activeSlot"], 1)

    def test_reconcile_corrects_registered_slot_without_persisting_itself(self):
        first = oauth_account(accountUuid="first-account")
        third = oauth_account(
            emailAddress="third@example.com",
            accountUuid="third-account",
            organizationUuid="third-org",
        )
        state = state_mod.default_state()
        state["activeSlot"] = 1
        state["slots"] = {
            "1": self.slot(first),
            "3": self.slot(third),
        }

        with (
            mock.patch.object(accounts, "read_live_oauth_account", return_value=third),
            mock.patch.object(state_mod, "save_state") as save,
        ):
            binding = accounts.reconcile_active_slot(state)

        self.assertEqual(binding["status"], "managed")
        self.assertEqual(binding["slot"], 3)
        self.assertTrue(binding["changed"])
        self.assertEqual(state["activeSlot"], 3)
        save.assert_not_called()

    def test_reconcile_preserves_state_when_live_identity_is_unknown(self):
        state = state_mod.default_state()
        state["activeSlot"] = 2
        with mock.patch.object(accounts, "read_live_oauth_account", return_value=None):
            binding = accounts.reconcile_active_slot(state)
        self.assertEqual(binding["status"], "unknown")
        self.assertEqual(state["activeSlot"], 2)

    def test_unmanaged_live_account_requires_force_before_overwrite(self):
        target = oauth_account()
        unmanaged = oauth_account(
            emailAddress="unmanaged@example.com",
            accountUuid="unmanaged-account",
            organizationUuid="unmanaged-org",
        )
        state = state_mod.default_state()
        state["slots"]["1"] = self.slot(target)

        with (
            mock.patch.object(locks, "proper_lockfile", return_value=nullcontext()),
            mock.patch.object(creds, "kc_read_active", return_value="live-credentials"),
            mock.patch.object(accounts, "read_live_oauth_account", return_value=unmanaged),
            mock.patch.object(creds, "kc_write_active") as write_active,
        ):
            with self.assertRaisesRegex(errors.SwitchError, "강제 선택"):
                switch.perform_switch(state, 1)

        write_active.assert_not_called()

    def test_force_explicitly_replaces_unmanaged_live_account(self):
        target = oauth_account()
        unmanaged = oauth_account(
            emailAddress="unmanaged@example.com",
            accountUuid="unmanaged-account",
            organizationUuid="unmanaged-org",
        )
        state = state_mod.default_state()
        state["slots"]["1"] = self.slot(target)
        target_blob = "target-credentials"

        with (
            mock.patch.object(locks, "proper_lockfile", return_value=nullcontext()),
            mock.patch.object(
                creds, "kc_read_active", side_effect=["live-credentials", target_blob]
            ),
            mock.patch.object(accounts, "read_live_oauth_account", return_value=unmanaged),
            mock.patch.object(creds, "kc_read_slot", return_value=target_blob),
            mock.patch.object(creds, "kc_write_slot") as write_slot,
            mock.patch.object(creds, "kc_write_active") as write_active,
            mock.patch.object(switch, "splice_oauth_account"),
            mock.patch.object(switch, "bump_creds_mtime"),
            mock.patch.object(state_mod, "save_state"),
            mock.patch.object(logutil, "log"),
        ):
            outcome = switch.perform_switch(state, 1, force=True)

        self.assertEqual(outcome, "switched")
        self.assertEqual(state["activeSlot"], 1)
        write_slot.assert_not_called()
        write_active.assert_called_once_with(target_blob)


class SlotAliasTests(unittest.TestCase):
    def test_alias_is_trimmed_and_can_be_cleared_without_credentials_write(self):
        state = state_mod.default_state()
        state["slots"]["2"] = {
            "email": "person@example.com",
            "oauthAccount": oauth_account(),
            "enabled": True,
            "kind": "oauth",
        }
        with (
            mock.patch.object(state_mod, "save_state") as save,
            mock.patch.object(keychain, "kc_write") as keychain_write,
        ):
            label = accounts.set_slot_alias(state, 2, "  Work  ")
            cleared = accounts.set_slot_alias(state, 2, "   ")

        self.assertEqual(label, "Work")
        self.assertEqual(cleared, "person@example.com")
        self.assertIsNone(state["slots"]["2"]["alias"])
        self.assertEqual(save.call_count, 2)
        keychain_write.assert_not_called()

    def test_alias_rejects_unregistered_slot_and_more_than_80_characters(self):
        state = state_mod.default_state()
        with self.assertRaisesRegex(errors.LimitPilotError, "등록"):
            accounts.set_slot_alias(state, 1, "name")

        state["slots"]["1"] = {"email": "person@example.com"}
        with mock.patch.object(state_mod, "save_state") as save:
            with self.assertRaisesRegex(errors.LimitPilotError, "80"):
                accounts.set_slot_alias(state, 1, "x" * 81)
        save.assert_not_called()


class LegacySetupTokenSafetyTests(unittest.TestCase):
    @staticmethod
    def state_with_legacy_candidate():
        state = state_mod.default_state()
        state["activeSlot"] = 1
        state["slots"] = {
            "1": {
                "email": "active@example.com",
                "oauthAccount": oauth_account(
                    emailAddress="active@example.com",
                    accountUuid="active-account",
                ),
                "enabled": True,
                "kind": "oauth",
            },
            "2": {
                "email": "setup-token-2@token.local",
                "oauthAccount": {"emailAddress": "setup-token-2@token.local"},
                "enabled": True,
                "kind": "setup-token",
            },
        }
        return state

    def test_legacy_token_is_not_an_automatic_candidate(self):
        state = self.state_with_legacy_candidate()
        self.assertIsNone(decide.best_candidate(state, 1, {}))

        usage = {"five_hour": {"pct": 100.0}, "seven_day": {"pct": 100.0}}
        action, target, _ = decide.decide(state, {1: usage, 2: None})
        self.assertEqual(action, "blocked")
        self.assertIsNone(target)

    def test_legacy_token_cannot_be_switched_to_explicitly(self):
        state = self.state_with_legacy_candidate()
        with self.assertRaises(errors.SwitchError):
            switch.perform_switch(state, 2, dry_run=True)

    def test_collect_usage_skips_legacy_keychain_and_usage_request(self):
        state = self.state_with_legacy_candidate()
        state["activeSlot"] = 2
        state["slots"].pop("1")

        with (
            mock.patch.object(creds, "kc_read_active") as read_active,
            mock.patch.object(creds, "kc_read_slot") as read_slot,
            mock.patch.object(usage_api, "fetch_usage") as fetch,
        ):
            result = collector.collect_usage(state)

        self.assertEqual(result, {2: None})
        read_active.assert_not_called()
        read_slot.assert_not_called()
        fetch.assert_not_called()

    def test_dashboard_marks_legacy_without_keychain_or_usage_request(self):
        state = self.state_with_legacy_candidate()

        with (
            mock.patch.object(creds, "kc_read_active") as read_active,
            mock.patch.object(creds, "kc_read_slot") as read_slot,
            mock.patch.object(status_mod, "fetch_raw_usage") as fetch,
        ):
            status = status_mod.slot_status(state, 2, active=False)

        self.assertTrue(status["registered"])
        self.assertEqual(status["kind"], "setup-token")
        self.assertEqual(status["error"], "limited-token")
        self.assertEqual(status["windows"], [])
        read_active.assert_not_called()
        read_slot.assert_not_called()
        fetch.assert_not_called()

    def test_delete_keeps_slot_when_keychain_delete_fails(self):
        state = self.state_with_legacy_candidate()
        with (
            mock.patch.object(subprocess, "run") as run,
            mock.patch.object(state_mod, "save_state") as save,
        ):
            run.return_value.returncode = 1
            with self.assertRaises(errors.KeychainError):
                accounts.delete_slot(state, 2)

        self.assertIn("2", state["slots"])
        save.assert_not_called()

    def test_delete_removes_slot_after_keychain_item_is_absent(self):
        state = self.state_with_legacy_candidate()
        with (
            mock.patch.object(subprocess, "run") as run,
            mock.patch.object(state_mod, "save_state") as save,
            mock.patch.object(logutil, "log"),
        ):
            run.return_value.returncode = constants.KC_NOT_FOUND_RC
            accounts.delete_slot(state, 2)

        self.assertNotIn("2", state["slots"])
        save.assert_called_once_with(state)


class DashboardAddSessionTests(unittest.TestCase):
    def setUp(self):
        server._add_session = None
        server._add_status.clear()
        server._add_status.update({"status": "idle"})
        # 메시지 언어는 실행 머신의 실제 state.json에 좌우되면 안 된다 — ko 고정
        patcher = mock.patch.object(status_mod, "_lang", return_value="ko")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        server.cancel_add(force=True)

    def test_old_worker_cannot_clear_new_session_or_status(self):
        class OldSession:
            closed = False

            def wait_for_credentials(self):
                raise errors.LimitPilotError("old session stopped")

            def close(self):
                self.closed = True

        old = OldSession()
        new = mock.Mock()
        server._add_session = new
        server._add_status.update(
            {"status": "running", "sessionId": "new", "message": "new session"}
        )

        server._add_worker(old, None, "old")

        self.assertIs(server._add_session, new)
        self.assertEqual(server._add_status["message"], "new session")
        self.assertTrue(old.closed)

    def test_superseded_success_cannot_register_account(self):
        old = mock.Mock()
        old.wait_for_credentials.return_value = (oauth_blob(), oauth_account())
        new = mock.Mock()
        server._add_session = new
        server._add_status.update(
            {"status": "running", "sessionId": "new", "message": "new session"}
        )

        with mock.patch.object(accounts, "register_oauth_slot") as register:
            server._add_worker(old, None, "old")

        register.assert_not_called()
        self.assertIs(server._add_session, new)
        self.assertEqual(server._add_status["message"], "new session")

    def test_success_status_keeps_own_session_id(self):
        session = mock.Mock()
        session.wait_for_credentials.return_value = (oauth_blob(), oauth_account())
        server._add_session = session
        server._add_status.update({"status": "running", "sessionId": "current"})

        with (
            mock.patch.object(locks, "state_lock", return_value=nullcontext()),
            mock.patch.object(state_mod, "load_state", return_value=state_mod.default_state()),
            mock.patch.object(
                accounts, "register_oauth_slot", return_value=(3, "work", True)
            ),
        ):
            server._add_worker(session, "work", "current")

        self.assertIsNone(server._add_session)
        self.assertEqual(server._add_status["status"], "done")
        self.assertEqual(server._add_status["sessionId"], "current")
        session.close.assert_called_once_with()

    def test_cancel_closes_current_session(self):
        session = mock.Mock()
        server._add_session = session
        server._add_status.update({"status": "running", "sessionId": "current"})

        message = server.cancel_add("current")

        self.assertIn("취소", message)
        session.close.assert_called_once_with()
        self.assertIsNone(server._add_session)
        self.assertEqual(server._add_status, {"status": "idle"})

    def test_stale_cancel_cannot_close_new_session(self):
        session = mock.Mock()
        server._add_session = session
        server._add_status.update({"status": "running", "sessionId": "new"})

        with self.assertRaisesRegex(errors.LimitPilotError, "이전 취소 요청"):
            server.cancel_add("old")

        session.close.assert_not_called()
        self.assertIs(server._add_session, session)

    def test_stale_code_cannot_feed_new_session(self):
        session = mock.Mock()
        server._add_session = session
        server._add_status.update({"status": "running", "sessionId": "new"})

        with self.assertRaisesRegex(errors.LimitPilotError, "세션이 바뀌었습니다"):
            server.feed_add("code#state", "old")

        session.feed.assert_not_called()

    def test_active_card_marks_unregistered_live_identity_as_unmanaged(self):
        registered = oauth_account(
            emailAddress="registered@example.com",
            accountUuid="registered-account",
            organizationUuid="registered-org",
        )
        live = oauth_account(
            emailAddress="live@example.com",
            accountUuid="live-account",
            organizationUuid="live-org",
        )
        state = state_mod.default_state()
        state["activeSlot"] = 1
        state["slots"]["1"] = {
            "email": registered["emailAddress"],
            "oauthAccount": registered,
            "enabled": True,
            "kind": "oauth",
        }
        with (
            mock.patch.object(status_mod, "_cache", {}),
            mock.patch.object(filestore, "read_json", return_value={"oauthAccount": live}),
            mock.patch.object(creds, "kc_read_active") as read_active,
            mock.patch.object(status_mod, "fetch_raw_usage") as fetch,
        ):
            status = status_mod.slot_status(state, 1, True)

        self.assertEqual(status["error"], "unmanaged")
        read_active.assert_not_called()
        fetch.assert_not_called()


class SnapshotFirstDashboardTests(unittest.TestCase):
    """스냅샷-우선 게이트는 usage API 네트워크 조회만 생략해야 한다 —
    신원(unmanaged)·토큰 만료 같은 로컬 검사는 스냅샷 신선도와 무관하게 수행."""

    @staticmethod
    def _fresh_slot(**extra):
        usage = usage_api.normalize_usage({
            "limits": [
                {"kind": "session", "percent": 42, "resets_at": "2099-01-01T00:00:00Z"},
                {"kind": "weekly_all", "percent": 12, "resets_at": "2099-01-02T00:00:00Z"},
                {"kind": "weekly_scoped", "percent": 26,
                 "resets_at": "2099-01-03T00:00:00Z",
                 "scope": {"model": {"display_name": "Opus"}}},
            ],
        })
        return {
            "email": "a@example.com",
            "oauthAccount": oauth_account(emailAddress="a@example.com"),
            "enabled": True,
            "kind": "oauth",
            "usage": usage,
            "usageFetchedAt": time.time() - 30,
            **extra,
        }

    def test_fresh_daemon_snapshot_skips_live_usage_fetch(self):
        state = state_mod.default_state()
        state["slots"]["2"] = self._fresh_slot()

        with (
            mock.patch.object(status_mod, "_cache", {}),
            mock.patch.object(creds, "kc_read_slot", return_value="blob"),
            mock.patch.object(oauth_mod, "parse_oauth", return_value={"accessToken": "tok"}),
            mock.patch.object(oauth_mod, "is_expired", return_value=False),
            mock.patch.object(status_mod, "fetch_raw_usage") as fetch,
        ):
            status = status_mod.slot_status(state, 2, active=False)

        fetch.assert_not_called()
        self.assertIsNone(status["error"])
        self.assertEqual([w["label"] for w in status["windows"]],
                         ["세션 (5시간)", "주간 (7일)", "주간 · Opus"])

    def test_stale_snapshot_falls_back_to_live_fetch(self):
        state = state_mod.default_state()
        state["slots"]["2"] = self._fresh_slot(usageFetchedAt=time.time() - 700)

        with (
            mock.patch.object(status_mod, "_cache", {}),
            mock.patch.object(creds, "kc_read_slot", return_value="blob"),
            mock.patch.object(oauth_mod, "parse_oauth", return_value={"accessToken": "tok"}),
            mock.patch.object(oauth_mod, "is_expired", return_value=False),
            mock.patch.object(status_mod, "fetch_raw_usage",
                              return_value=(None, "http-429")) as fetch,
        ):
            status = status_mod.slot_status(state, 2, active=False)

        fetch.assert_called_once()
        self.assertEqual(status["error"], "http-429")

    def test_daemon_backoff_suppresses_dashboard_fallback(self):
        # 데몬이 429 백오프로 미룬 조회를 대시보드가 대신 때리면 안 된다
        state = state_mod.default_state()
        state["slots"]["2"] = self._fresh_slot(
            usageFetchedAt=time.time() - 700,
            fetchBackoff={"fails": 2, "until": time.time() + 300})

        with (
            mock.patch.object(status_mod, "_cache", {}),
            mock.patch.object(creds, "kc_read_slot", return_value="blob"),
            mock.patch.object(oauth_mod, "parse_oauth", return_value={"accessToken": "tok"}),
            mock.patch.object(oauth_mod, "is_expired", return_value=False),
            mock.patch.object(status_mod, "fetch_raw_usage") as fetch,
        ):
            status = status_mod.slot_status(state, 2, active=False)

        fetch.assert_not_called()
        self.assertTrue(status["windows"])  # 스냅샷은 그대로 표시

    def test_active_card_marks_unmanaged_even_with_fresh_snapshot(self):
        live = oauth_account(
            emailAddress="live@example.com",
            accountUuid="live-account",
            organizationUuid="live-org",
        )
        state = state_mod.default_state()
        state["activeSlot"] = 1
        state["slots"]["1"] = self._fresh_slot()

        with (
            mock.patch.object(status_mod, "_cache", {}),
            mock.patch.object(filestore, "read_json", return_value={"oauthAccount": live}),
            mock.patch.object(creds, "kc_read_active") as read_active,
            mock.patch.object(status_mod, "fetch_raw_usage") as fetch,
        ):
            status = status_mod.slot_status(state, 1, True)

        self.assertEqual(status["error"], "unmanaged")
        read_active.assert_not_called()
        fetch.assert_not_called()

    def test_expired_active_token_shows_badge_despite_fresh_snapshot(self):
        # 데몬이 남긴 만료 시각으로 판단한다 — 신선한 스냅샷이면 저장소를 읽지 않는다
        state = state_mod.default_state()
        state["activeSlot"] = 1
        slot = self._fresh_slot(tokenExpiresAt=(time.time() - 60) * 1000)
        state["slots"]["1"] = slot

        with (
            mock.patch.object(status_mod, "_cache", {}),
            mock.patch.object(filestore, "read_json",
                              return_value={"oauthAccount": slot["oauthAccount"]}),
            mock.patch.object(accounts, "find_slot_by_identity", return_value=1),
            mock.patch.object(creds, "kc_read_active",
                              side_effect=AssertionError("store read during fresh poll")),
            mock.patch.object(status_mod, "fetch_raw_usage") as fetch,
        ):
            status = status_mod.slot_status(state, 1, True)

        self.assertTrue(status["tokenExpired"])
        fetch.assert_not_called()

    def test_collector_records_token_expiry_without_the_token(self):
        state = state_mod.default_state()
        state["activeSlot"] = 2
        state["slots"]["1"] = {"email": "a@example.com", "enabled": True}
        future = (time.time() + 7200) * 1000
        with (
            mock.patch.object(creds, "kc_read_slot", return_value="blob"),
            mock.patch.object(oauth_mod, "parse_oauth",
                              return_value={"accessToken": "fake-access-value",
                                            "expiresAt": future}),
        ):
            oauth, err = collector.slot_access_oauth(state, 1)
        self.assertIsNone(err)
        self.assertEqual(state["slots"]["1"]["tokenExpiresAt"], future)
        self.assertNotIn("fake-access-value", json.dumps(state))


if __name__ == "__main__":
    unittest.main()
