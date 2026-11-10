"""AC-2: the practice tour runs the real decision/switch code against fake I/O only."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from limitpilot import demo
from limitpilot import paths
from limitpilot import sandbox
from limitpilot import switch
from limitpilot import usage_api
from limitpilot.demo import tour
from limitpilot.policy import decide as decide_mod
from limitpilot.store import fake
from tests.support import ROOT, SRC


def _repo_snapshot():
    """(path, size, mtime) of every file in the repo outside .git — to prove nothing was written."""
    out = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            full = os.path.join(base, name)
            st = os.stat(full)
            out.append((full, st.st_size, st.st_mtime_ns))
    return sorted(out)


class TourSubprocessTests(unittest.TestCase):
    def test_tour_output_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, PYTHONPATH=SRC, PYTHONDONTWRITEBYTECODE="1",
                       LIMITPILOT_DEMO_ROOT=os.path.join(tmp, "limitpilot-demo"))
            for key in ("LIMITPILOT_HOME", "LIMITPILOT_CLAUDE_HOME", "LIMITPILOT_LANG"):
                env.pop(key, None)
            before = _repo_snapshot()
            result = subprocess.run(
                [sys.executable, "-B", "-m", "limitpilot", "--demo", "--lang", "en", "tour"],
                capture_output=True, text=True, env=env, timeout=120, cwd=tmp)
            after = _repo_snapshot()
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("switch slot 1 -> slot 2", out)
        self.assertIn("session 5h 91% >= 90%", out)
        self.assertIn("slot 3 excluded: needs login", out)
        self.assertIn("practice (mock) mode", out)
        self.assertEqual(out.strip().splitlines()[-1],
                         "sandbox: real keychain 0, network 0, launchctl 0")
        self.assertEqual(before, after, "the tour must not write into the repository")


class TourInProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="limitpilot-demo-test-")
        self.env = mock.patch.dict(os.environ, {
            "LIMITPILOT_DEMO_ROOT": os.path.join(self.tmp, "limitpilot-demo"),
            "LIMITPILOT_LANG": "en"})
        self.env.start()
        self.saved = {k: getattr(paths, k) for k in (
            "HOME", "STATE_DIR", "CLAUDE_JSON", "CLAUDE_SETTINGS_FILE", "CC_CREDS_FILE",
            "CRED_LOCK_DIR", "CONF_LOCK_DIR", "LAUNCHD_PLIST", "_CLAUDE_BIN_CANDIDATES")}

    def tearDown(self):
        # 연습 모드가 바꾼 프로세스 상태를 테스트 전 상태로 되돌린다
        for k, v in self.saved.items():
            setattr(paths, k, v)
        fake.deactivate()
        usage_api.set_source(None)
        for key in ("LIMITPILOT_DEMO", "LIMITPILOT_QUIET"):
            os.environ.pop(key, None)
        self.env.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_real_decide_and_switch_run_and_fake_active_store_changes(self):
        counts_before = sandbox.counts()
        with (
            mock.patch.object(decide_mod, "decide", wraps=decide_mod.decide) as decide_spy,
            mock.patch.object(switch, "perform_switch", wraps=switch.perform_switch) as switch_spy,
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(tour.run(), 0)
        self.assertGreaterEqual(decide_spy.call_count, 4)          # 틱마다 실제 결정 함수
        self.assertEqual(switch_spy.call_count, 1)                 # 실제 전환 1회
        self.assertEqual(switch_spy.call_args.args[1], 2)
        # 가짜 '활성' 저장소에는 이제 슬롯 2 의 토큰이 들어 있다(회전된 값)
        active = json.loads(fake.read(paths.ACTIVE_SERVICE, paths.KC_USER))
        self.assertTrue(active["claudeAiOauth"]["accessToken"].startswith("demo-access-2"))
        # 가짜 ~/.claude.json 의 oauthAccount 도 슬롯 2 계정으로 바뀌었다
        with open(paths.CLAUDE_JSON, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["oauthAccount"]["emailAddress"],
                             "personal@example.com")
        # 모든 파일은 연습 폴더 안에만 생긴다
        self.assertTrue(paths.STATE_DIR.startswith(self.tmp))
        self.assertTrue(paths.HOME.startswith(self.tmp))
        self.assertEqual(sandbox.counts(), counts_before)          # 실제 호출 시도 0
        self.assertIn("switch slot 1 -> slot 2", out.getvalue())

    def test_demo_folder_reset_refuses_foreign_folders(self):
        foreign = os.path.join(self.tmp, "important")
        os.makedirs(foreign)
        with open(os.path.join(foreign, "keep.txt"), "w") as handle:
            handle.write("x")
        with self.assertRaises(RuntimeError):
            demo.build_home(foreign)
        self.assertTrue(os.path.exists(os.path.join(foreign, "keep.txt")))


if __name__ == "__main__":
    unittest.main()
