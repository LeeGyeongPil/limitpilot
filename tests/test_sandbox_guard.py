"""The sandbox switch blocks real credential, daemon and network calls (and counts them)."""

import os
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request

from limitpilot import paths
from limitpilot import sandbox
from limitpilot.store import keychain
from tests.support import SRC


class SandboxGuardTests(unittest.TestCase):
    def test_guard_is_active_for_the_whole_suite(self):
        self.assertTrue(sandbox.installed())
        self.assertEqual(os.environ.get("LIMITPILOT_SANDBOX"), "1")

    def test_real_security_tool_is_blocked_and_counted(self):
        before = sandbox.counts()["keychain"]
        with sandbox.expecting(), self.assertRaises(sandbox.SandboxViolation):
            keychain.kc_read(paths.ACTIVE_SERVICE, paths.KC_USER)
        self.assertEqual(sandbox.counts()["keychain"], before + 1)

    def test_launchctl_and_osascript_are_blocked(self):
        for argv in (["launchctl", "print", "gui/0"], ["/usr/bin/osascript", "-e", "1"],
                     ["schtasks", "/Query"]):
            with sandbox.expecting(), self.assertRaises(sandbox.SandboxViolation):
                subprocess.run(argv, capture_output=True)

    def test_external_network_is_blocked(self):
        with sandbox.expecting(), self.assertRaises(sandbox.SandboxViolation):
            urllib.request.urlopen("https://api.anthropic.com/api/oauth/usage", timeout=1)
        with sandbox.expecting(), self.assertRaises(sandbox.SandboxViolation):
            socket.create_connection(("192.0.2.1", 443), timeout=0.1)

    def test_loopback_is_allowed(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        try:
            client = socket.create_connection(server.getsockname(), timeout=2)
            client.close()
        finally:
            server.close()

    def test_fake_executables_in_temp_are_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_tool = os.path.join(tmp, "security")
            with open(fake_tool, "w") as handle:
                handle.write("#!/bin/sh\necho fake\n")
            os.chmod(fake_tool, 0o700)
            out = subprocess.run([fake_tool], capture_output=True, text=True)
            self.assertEqual(out.stdout.strip(), "fake")

    def test_writes_under_the_real_home_are_blocked(self):
        target = os.path.join(sandbox._real_home(), ".limitpilot", "sandbox-probe.json")
        with sandbox.expecting(), self.assertRaises(sandbox.SandboxViolation):
            with open(target, "w") as handle:
                handle.write("x")
        self.assertFalse(os.path.exists(target))

    def test_paths_never_default_to_the_real_home_in_the_sandbox(self):
        real = sandbox._real_home()
        self.assertFalse(paths.STATE_DIR.startswith(os.path.join(real, ".limitpilot")))
        self.assertNotEqual(os.path.realpath(paths.HOME), os.path.realpath(real))

    def test_subprocesses_inherit_the_guard(self):
        code = ("import limitpilot, subprocess\n"
                "from limitpilot import sandbox\n"
                "try:\n"
                "    subprocess.run(['launchctl', 'list'], capture_output=True)\n"
                "except sandbox.SandboxViolation:\n"
                "    print('blocked')\n")
        env = dict(os.environ, PYTHONPATH=SRC, PYTHONDONTWRITEBYTECODE="1")
        out = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True,
                             env=env, timeout=60)
        self.assertEqual(out.stdout.strip(), "blocked", out.stderr)

    def test_summary_line_format(self):
        self.assertRegex(sandbox.summary_line(),
                         r"^sandbox: real keychain \d+, network \d+, launchctl \d+$")


if __name__ == "__main__":
    unittest.main()
