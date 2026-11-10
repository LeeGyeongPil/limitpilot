"""AC-3: entry points, the zipapp daemon copy, package resources and one version source."""

import os
import plistlib
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile

import limitpilot
from limitpilot import constants
from limitpilot.daemon import deploy
from limitpilot.dashboard import server
from limitpilot import widget
from tests.support import ROOT, SRC


def _pyproject() -> str:
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as handle:
        return handle.read()


def _env():
    env = dict(os.environ, PYTHONPATH=SRC, PYTHONDONTWRITEBYTECODE="1")
    return env


class EntryPointTests(unittest.TestCase):
    def test_python_m_prints_version(self):
        out = subprocess.run([sys.executable, "-B", "-m", "limitpilot", "--version"],
                             capture_output=True, text=True, env=_env(), timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), f"limitpilot {limitpilot.__version__}")

    def test_console_script_points_at_cli_main(self):
        text = _pyproject()
        self.assertIn('limitpilot = "limitpilot.cli:main"', text)
        self.assertIn('dependencies = []', text)          # 런타임 의존성 0
        self.assertIn('requires-python = ">=3.10"', text)

    def test_no_command_prints_help_and_fails(self):
        out = subprocess.run([sys.executable, "-B", "-m", "limitpilot"],
                             capture_output=True, text=True, env=_env(), timeout=60)
        self.assertEqual(out.returncode, 1)
        self.assertIn("usage: limitpilot", out.stdout)
        self.assertNotIn("sync-daemon", out.stdout)   # 숨김 명령은 도움말에 없다


class ParserTests(unittest.TestCase):
    def test_dashboard_options_pass_through_to_the_server(self):
        from unittest import mock

        from limitpilot import cli

        args = cli.build_parser().parse_args(
            ["--demo", "dashboard", "--no-open", "--port", "18750", "--add"])
        self.assertTrue(args.demo and args.no_open and args.add)
        with mock.patch.object(server, "main", return_value=0) as main:
            self.assertEqual(cli.cmd_dashboard(args), 0)
        argv = main.call_args.args[0]
        self.assertEqual(argv[:2], ["--port", "18750"])
        self.assertIn("--no-open", argv)
        self.assertIn("--add", argv)

    def test_install_daemon_accepts_enable_auto(self):
        from limitpilot import cli

        args = cli.build_parser().parse_args(["install-daemon", "--enable-auto"])
        self.assertTrue(args.enable_auto)


class ZipappTests(unittest.TestCase):
    def test_pyz_runs_and_reads_dashboard_html_from_inside(self):
        with tempfile.TemporaryDirectory() as tmp:
            pyz = deploy.write_pyz(os.path.join(tmp, "limitpilot.pyz"))
            out = subprocess.run([sys.executable, "-B", pyz, "--version"],
                                 capture_output=True, text=True, env=dict(os.environ), timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), f"limitpilot {limitpilot.__version__}")
            probe = ("import sys; sys.path.insert(0, sys.argv[1]);"
                     "from limitpilot.dashboard import server;"
                     "html = server.read_html();"
                     "print(len(html) > 1000, b'__DASHBOARD_TOKEN__' in html, server.__file__)")
            out = subprocess.run([sys.executable, "-B", "-c", probe, pyz],
                                 capture_output=True, text=True, env=dict(os.environ), timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue(out.stdout.startswith("True True "), out.stdout)
            self.assertIn("limitpilot.pyz", out.stdout)       # 정말 pyz 안에서 읽었다
            with zipfile.ZipFile(pyz) as zf:
                names = zf.namelist()
            self.assertIn("limitpilot/native/macos/main.swift", names)
            self.assertIn("limitpilot/demo/scenario.json", names)

    def test_running_from_pyz_reuses_the_archive_as_daemon_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            pyz = deploy.write_pyz(os.path.join(tmp, "limitpilot.pyz"))
            probe = ("import sys; sys.path.insert(0, sys.argv[1]);"
                     "from limitpilot.daemon import deploy;"
                     "print(deploy.running_archive() == sys.argv[1],"
                     " deploy.build_pyz_bytes() == open(sys.argv[1], 'rb').read())")
            out = subprocess.run([sys.executable, "-B", "-c", probe, pyz],
                                 capture_output=True, text=True, env=dict(os.environ), timeout=60)
            self.assertEqual(out.stdout.strip(), "True True", out.stderr)


class VersionSourceTests(unittest.TestCase):
    def test_pyproject_version_equals_package_version(self):
        text = _pyproject()
        m = re.search(r'^version = "([^"]+)"', text, re.M)
        self.assertEqual(m.group(1), limitpilot.__version__)

    def test_info_plist_user_agent_and_health_use_the_same_version(self):
        info = plistlib.loads(widget.render_info_plist(python="/usr/bin/python3"))
        self.assertEqual(info["CFBundleShortVersionString"], limitpilot.__version__)
        self.assertEqual(info["CFBundleVersion"], limitpilot.__version__)
        self.assertEqual(info["CFBundleIdentifier"], "io.github.limitpilot")
        self.assertEqual(info["CFBundleExecutable"], "LimitPilot")
        self.assertEqual(constants.USER_AGENT, f"limitpilot/{limitpilot.__version__}")
        self.assertEqual(server.APP_VERSION, limitpilot.__version__)

    def test_widget_dry_run_lists_bundle_contents(self):
        import io
        import contextlib
        import types

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            widget.run(types.SimpleNamespace(action="build", out="/tmp/x", dry_run=True))
        self.assertIn("limitpilot.pyz", buf.getvalue())
        self.assertIn("io.github.limitpilot", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
