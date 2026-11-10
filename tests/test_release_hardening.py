import io
import os
import plistlib
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from limitpilot import cli
from limitpilot import constants
from limitpilot import errors
from limitpilot import locks
from limitpilot import logutil
from limitpilot import paths
from limitpilot import purge
from limitpilot import state as state_mod
from limitpilot import switch
from limitpilot import tick
from limitpilot.daemon import deploy as deploy_mod
from limitpilot.daemon import launchd
from limitpilot.daemon import schtasks
from limitpilot.dashboard import runtime as runtime_mod
from limitpilot.dashboard import server as server_mod
from limitpilot.store import creds


ROOT = Path(__file__).resolve().parents[1]


NATIVE = ROOT / "src" / "limitpilot" / "native"


class VersionConsistencyTests(unittest.TestCase):
    def test_runtime_and_bundle_versions_have_one_source(self):
        from limitpilot import widget

        self.assertRegex(constants.APP_VERSION, r"^\d+\.\d+\.\d+$")
        self.assertEqual(constants.USER_AGENT, f"limitpilot/{constants.APP_VERSION}")
        info = plistlib.loads(widget.render_info_plist())
        self.assertEqual(info["CFBundleShortVersionString"], constants.APP_VERSION)
        self.assertEqual(info["CFBundleVersion"], constants.APP_VERSION)

    def test_cli_reports_the_same_version(self):
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["limitpilot", "--version"]),
            redirect_stdout(output),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main()
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"limitpilot {constants.APP_VERSION}")


class WindowsTrayExitTests(unittest.TestCase):
    def test_normal_message_loop_exit_keeps_scheduled_daemon(self):
        # 트레이를 닫아도 감시 데몬은 그대로 — 자동 전환을 끄는 것은 별도 메뉴 항목이다
        source = (NATIVE / "windows/tray.ps1").read_text(encoding="utf-8-sig")
        run_at = source.index("[System.Windows.Forms.Application]::Run")
        tail = source[run_at:]
        self.assertNotIn("uninstall-daemon", tail)
        self.assertIn("$script:Tray.Dispose()", tail)
        self.assertIn('"uninstall-daemon"', source)  # "자동 전환도 끄고 종료" 항목에만


class PurgeTests(unittest.TestCase):
    def test_windows_purge_removes_tool_data_but_preserves_active_claude(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            state = home / ".limitpilot"
            (state / "slots").mkdir(parents=True)
            (state / "slots/slot-1.json").write_text("backup-token", encoding="utf-8")
            (state / "limitpilot.log").write_text("log", encoding="utf-8")
            active = home / ".claude/.credentials.json"
            active.parent.mkdir()
            active.write_text("active-token", encoding="utf-8")
            claude_json = home / ".claude.json"
            claude_json.write_text('{"history": true}', encoding="utf-8")

            def assert_purge_barrier_is_already_visible():
                self.assertTrue((state / ".purging").exists())

            with (
                mock.patch.object(paths, "HOME", str(home)),
                mock.patch.object(paths, "STATE_DIR", str(state)),
                mock.patch.object(paths, "IS_WIN", True),
                mock.patch.object(locks, "state_lock", return_value=nullcontext()),
                mock.patch.object(
                    runtime_mod, "_stop_running_dashboards",
                    side_effect=assert_purge_barrier_is_already_visible,
                ) as stop_dashboards,
                mock.patch.object(runtime_mod, "_dashboard_process_pids", return_value=[]),
                mock.patch.object(deploy_mod, "_stop_daemon") as stop_daemon,
                mock.patch.object(creds, "kc_read_active",
                                  side_effect=AssertionError("active credential read")),
                mock.patch.object(creds, "kc_write_active",
                                  side_effect=AssertionError("active credential write")),
                mock.patch.object(switch, "splice_oauth_account",
                                  side_effect=AssertionError("Claude config write")),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(purge.cmd_purge(types.SimpleNamespace(yes=True)), 0)

            self.assertFalse(state.exists())
            self.assertEqual(active.read_text(encoding="utf-8"), "active-token")
            self.assertEqual(claude_json.read_text(encoding="utf-8"),
                             '{"history": true}')
            stop_dashboards.assert_called_once_with()
            stop_daemon.assert_called_once_with(strict=True)

    def test_install_refuses_incomplete_prior_purge(self):
        with tempfile.TemporaryDirectory() as state:
            Path(state, ".purging").touch()
            with (
                mock.patch.object(paths, "STATE_DIR", state),
                mock.patch.object(deploy_mod, "deploy_daemon_script") as deploy,
            ):
                with self.assertRaisesRegex(errors.LimitPilotError, "purge --yes"):
                    cli.cmd_install_daemon(None)
            deploy.assert_not_called()

    def test_backup_keychain_deletion_repeats_until_service_is_empty(self):
        results = [
            mock.Mock(returncode=0),
            mock.Mock(returncode=0),
            mock.Mock(returncode=constants.KC_NOT_FOUND_RC),
            mock.Mock(returncode=constants.KC_NOT_FOUND_RC),
        ]
        for result in results:
            result.stderr = ""
        with (
            mock.patch.object(paths, "IS_WIN", False),
            mock.patch.object(subprocess, "run", side_effect=results) as run,
        ):
            self.assertEqual(purge._purge_backup_keychain_items(), 2)

        delete_argv = [
            paths.SECURITY, "delete-generic-password", "-s", paths.BACKUP_SERVICE,
        ]
        self.assertEqual([call.args[0] for call in run.call_args_list[:3]],
                         [delete_argv, delete_argv, delete_argv])
        self.assertEqual(run.call_args_list[3].args[0], [
            paths.SECURITY, "find-generic-password", "-s", paths.BACKUP_SERVICE,
        ])
        for call in run.call_args_list:
            self.assertNotIn(paths.ACTIVE_SERVICE, call.args[0])

    def test_keychain_failure_keeps_state_for_safe_retry(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            state = home / ".limitpilot"
            state.mkdir()
            marker = state / "state.json"
            marker.write_text("retry metadata", encoding="utf-8")
            with (
                mock.patch.object(paths, "HOME", str(home)),
                mock.patch.object(paths, "STATE_DIR", str(state)),
                mock.patch.object(locks, "state_lock", return_value=nullcontext()),
                mock.patch.object(runtime_mod, "_stop_running_dashboards"),
                mock.patch.object(deploy_mod, "_stop_daemon"),
                mock.patch.object(purge, "_purge_isolated_keychain_items"),
                mock.patch.object(
                    purge, "_purge_backup_keychain_items",
                    side_effect=errors.KeychainError("locked"),
                ),
            ):
                with self.assertRaises(errors.KeychainError):
                    purge.cmd_purge(types.SimpleNamespace(yes=True))
            self.assertEqual(marker.read_text(encoding="utf-8"), "retry metadata")

    def test_scheduler_query_failure_is_not_treated_as_task_absence(self):
        with (
            mock.patch.object(paths, "IS_WIN", True),
            mock.patch.object(
                subprocess, "run", return_value=mock.Mock(returncode=1, stdout=""),
            ),
        ):
            with self.assertRaisesRegex(errors.LimitPilotError, "상태를 확인"):
                schtasks._win_uninstall_daemon(strict=True)

    def test_launchctl_unknown_failure_is_not_treated_as_service_absence(self):
        with mock.patch.object(
            subprocess, "run", return_value=mock.Mock(returncode=1),
        ):
            with self.assertRaisesRegex(errors.LimitPilotError, "상태를 확인"):
                launchd._mac_uninstall_daemon(strict=True)

    def test_valid_live_dashboard_record_without_health_blocks_purge(self):
        record = ("runtime.json", 1234, 9123, "n" * 32, "i" * 24)
        with (
            mock.patch.object(runtime_mod, "_dashboard_runtime_records", return_value=[record]),
            mock.patch.object(runtime_mod, "_dashboard_health", return_value=None),
            mock.patch.object(runtime_mod, "_process_exists", return_value=True),
            mock.patch.object(time, "monotonic", side_effect=[0.0, 4.0]),
        ):
            with self.assertRaisesRegex(errors.LimitPilotError, "상태를 확인"):
                runtime_mod._stop_running_dashboards()

    def test_windows_process_probe_never_uses_os_kill(self):
        result = mock.Mock(returncode=0, stdout='"python.exe","1234","Console","1"\n')
        with (
            mock.patch.object(paths, "IS_WIN", True),
            mock.patch.object(subprocess, "run", return_value=result),
            mock.patch.object(os, "kill") as kill,
        ):
            self.assertTrue(runtime_mod._process_exists(1234))
        kill.assert_not_called()

    def test_process_scan_detects_dashboard_before_runtime_record(self):
        ps = mock.Mock(
            returncode=0,
            stdout=("4321 Python /usr/bin/python3 -m limitpilot dashboard --port 9999\n"
                    "4322 Python /usr/bin/python3 /tmp/other/dashboard.py\n"
                    "4323 limitpilot /opt/bin/limitpilot --demo dashboard --no-open\n"),
        )
        with (
            mock.patch.object(paths, "IS_WIN", False),
            mock.patch.object(subprocess, "run", return_value=ps),
        ):
            self.assertEqual(runtime_mod._dashboard_process_pids(), [4321, 4323])

    def test_windows_process_scan_uses_cim_command_lines(self):
        result = mock.Mock(returncode=0, stdout="4321\n")
        with (
            mock.patch.object(paths, "IS_WIN", True),
            mock.patch.object(subprocess, "run", return_value=result) as run,
        ):
            self.assertEqual(runtime_mod._dashboard_process_pids(), [4321])
        self.assertIn("Get-CimInstance", run.call_args.args[0][-1])
        self.assertIn("*limitpilot*dashboard*", run.call_args.args[0][-1])

    def test_purge_marker_makes_waiting_daemon_tick_exit_without_writes(self):
        with tempfile.TemporaryDirectory() as state:
            Path(state, ".purging").touch()
            with (
                mock.patch.object(paths, "STATE_DIR", state),
                mock.patch.object(locks, "state_lock", return_value=nullcontext()),
                mock.patch.object(state_mod, "load_state") as load,
                mock.patch.object(logutil, "log") as log,
            ):
                self.assertEqual(tick.run_tick(), (constants.EXIT_PURGING, None))
            load.assert_not_called()
            log.assert_not_called()

    def test_confirmation_must_match_exact_word(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            state = home / ".limitpilot"
            with (
                mock.patch.object(paths, "HOME", str(home)),
                mock.patch.object(paths, "STATE_DIR", str(state)),
                mock.patch("builtins.input", return_value="purge"),
                mock.patch.object(runtime_mod, "_stop_running_dashboards") as stop,
            ):
                self.assertEqual(purge.cmd_purge(types.SimpleNamespace(yes=False)), 0)
            stop.assert_not_called()

    def test_state_root_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            target = home / "target"
            target.mkdir()
            state = home / ".limitpilot"
            state.symlink_to(target, target_is_directory=True)
            with (
                mock.patch.object(paths, "HOME", str(home)),
                mock.patch.object(paths, "STATE_DIR", str(state)),
            ):
                with self.assertRaisesRegex(errors.LimitPilotError, "심볼릭 링크"):
                    purge.cmd_purge(types.SimpleNamespace(yes=True))
            self.assertTrue(target.exists())

    def test_registered_dashboard_is_stopped_before_purge(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / ".limitpilot"
            state.mkdir()
            server = server_mod.ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
            port = server.server_address[1]
            nonce = "n" * 32
            instance = "i" * 24
            server.shutdown_nonce = nonce
            server.instance_id = instance
            runtime = state / f"{paths.DASHBOARD_RUNTIME_PREFIX}123.json"
            runtime.write_text(
                '{"app":"%s","pid":%d,"port":%d,"nonce":"%s","instance":"%s"}'
                % (paths.DASHBOARD_APP_ID, os.getpid(), port, nonce, instance),
                encoding="utf-8",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with (
                    mock.patch.object(paths, "STATE_DIR", str(state)),
                    mock.patch.object(paths, "DASHBOARD_DEFAULT_PORT", port),
                ):
                    runtime_mod._stop_running_dashboards()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
            finally:
                server.shutdown()
                server.server_close()
                server_mod._shutdown_requested.clear()

    def test_dashboard_start_is_refused_while_purge_barrier_exists(self):
        with tempfile.TemporaryDirectory() as state:
            Path(state, ".purging").touch()
            with (
                mock.patch.object(paths, "STATE_DIR", state),
                mock.patch.object(sys, "argv", ["dashboard", "--no-open"]),
                mock.patch.object(server_mod, "ThreadingHTTPServer") as server,
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(server_mod.main(), 1)
            server.assert_not_called()


if __name__ == "__main__":
    unittest.main()
