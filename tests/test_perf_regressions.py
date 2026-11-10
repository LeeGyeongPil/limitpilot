"""AC-6: performance regressions stay fixed.

* dashboard polling with fresh snapshots reads the credential store 0 times,
* gateway token totals read only bytes appended since the last scan,
* 1,440 "no action" daemon ticks write nothing to stderr and keep logs under the cap,
* the state lock is free while the (slow) usage request is in flight.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from limitpilot import gateway_usage
from limitpilot import locks
from limitpilot import logutil
from limitpilot import paths
from limitpilot import state
from limitpilot import tick
from limitpilot.dashboard import status as status_mod
from limitpilot.store import creds
from limitpilot.store import keychain
from tests.support import (ROOT, FakeUsage, blob, fake_store, identity, iso, temp_home, usage,
                           usage_source)


def _write_claude_json(n: int) -> None:
    with open(paths.CLAUDE_JSON, "w", encoding="utf-8") as handle:
        json.dump({"oauthAccount": identity(n)}, handle)


def _state_with_slots(count: int, fetched_at: float, session: float = 20.0) -> dict:
    st = state.default_state()
    st["activeSlot"] = 1
    st["settings"] = {"threshold": 90}
    for n in range(1, count + 1):
        st["slots"][str(n)] = {"email": f"user{n}@example.com", "oauthAccount": identity(n),
                               "enabled": True, "kind": "oauth",
                               "usage": usage(session=session, weekly=10),
                               "usageFetchedAt": fetched_at}
    return st


class DashboardPollingTests(unittest.TestCase):
    def test_ten_polls_over_five_fresh_slots_read_the_store_zero_times(self):
        with temp_home():
            _write_claude_json(1)
            state.save_state(_state_with_slots(5, time.time() - 30))
            reads = []
            spy = mock.Mock(side_effect=lambda *a, **k: reads.append(a) or None)
            with (
                mock.patch.object(creds, "kc_read_active", spy),
                mock.patch.object(creds, "kc_read_slot", spy),
                mock.patch.object(keychain, "kc_read", spy),
                mock.patch.object(status_mod, "fetch_raw_usage",
                                  side_effect=AssertionError("no network for fresh slots")),
                mock.patch.object(status_mod, "_cache", {}),
            ):
                for _ in range(10):
                    payload = status_mod.build_status()
            self.assertEqual(reads, [])
            self.assertEqual(len([s for s in payload["slots"] if s.get("registered", True)]), 5)

    def test_stale_snapshot_still_reads_once_and_remembers_expiry(self):
        with temp_home():
            _write_claude_json(1)
            st = _state_with_slots(2, time.time() - 3600)
            with (
                mock.patch.object(creds, "kc_read_slot", return_value=blob("a2")) as read,
                mock.patch.object(status_mod, "fetch_raw_usage", return_value=(None, "http-429")),
                mock.patch.object(status_mod, "_cache", {}),
                mock.patch.object(status_mod, "_expiry_ms", {}),
            ):
                status_mod.slot_status(st, 2, active=False)
                status_mod.slot_status(st, 2, active=False)   # 실패 캐시 TTL 안 — 다시 안 읽음
            self.assertEqual(read.call_count, 1)


class GatewayIncrementalTests(unittest.TestCase):
    def _line(self, mid, out_tokens):
        return json.dumps({"timestamp": iso(-60), "message": {
            "id": mid, "model": "example-model-id",
            "usage": {"input_tokens": 10, "output_tokens": out_tokens}}}) + "\n"

    def test_second_scan_reads_only_appended_bytes(self):
        with temp_home():
            folder = os.path.join(paths.HOME, ".claude", "projects", "p")
            os.makedirs(folder)
            path = os.path.join(folder, "s.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                for i in range(200):
                    handle.write(self._line(f"m{i}", 5))
            slot = {"modelId": "example-model-id", "addedAt": iso(-3600)}
            gateway_usage.forget()
            stats = gateway_usage._scanner_stats
            start = stats["bytesRead"]
            first = gateway_usage.gateway_token_usage(slot)
            full = stats["bytesRead"] - start
            self.assertEqual(full, os.path.getsize(path))
            self.assertEqual(first["output"], 1000)
            added = self._line("m-new", 7) + self._line("m0", 9)   # 새 메시지 + 스트리밍 갱신
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(added)
            gateway_usage._gateway_usage_cache.clear()           # TTL 만료와 같은 효과
            mark = stats["bytesRead"]
            second = gateway_usage.gateway_token_usage(slot)
            self.assertEqual(stats["bytesRead"] - mark, len(added.encode()))
            self.assertEqual(second["output"], 1000 + 7 + (9 - 5))
            self.assertEqual(second["responses"], 201)
            gateway_usage._gateway_usage_cache.clear()
            mark = stats["bytesRead"]
            gateway_usage.gateway_token_usage(slot)
            self.assertEqual(stats["bytesRead"] - mark, 0)       # 바뀐 것이 없으면 0바이트
            gateway_usage.forget()

    def test_background_mode_returns_immediately(self):
        with temp_home():
            gateway_usage.forget()
            slot = {"modelId": "example-model-id", "addedAt": iso(-3600)}
            result = gateway_usage.gateway_token_usage(slot, background=True)
            self.assertEqual(result["total"], 0)
            deadline = time.time() + 5
            while gateway_usage._running and time.time() < deadline:
                time.sleep(0.01)
            gateway_usage.forget()


class DaemonLogTests(unittest.TestCase):
    def test_1440_no_op_ticks_write_nothing_to_stderr_and_logs_stay_capped(self):
        with temp_home(), fake_store(paths.HOME) as store:
            _write_claude_json(1)
            store.write(paths.ACTIVE_SERVICE, paths.KC_USER, blob("token-1"))
            state.save_state(_state_with_slots(1, time.time()))
            err = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"LIMITPILOT_DAEMON": "1"}),
                mock.patch.object(logutil, "LOG_MAX_BYTES", 16 * 1024),
                mock.patch.object(os, "fsync"),                 # 속도만 — 동작은 같다
                contextlib.redirect_stderr(err),
            ):
                for _ in range(1440):
                    rc, _ = tick.run_tick()
                    self.assertEqual(rc, 2)                     # EXIT_NO_ACTION
                total = logutil.total_log_bytes()
                cap = logutil.LOG_MAX_BYTES * (logutil.LOG_BACKUPS + 1)
            self.assertEqual(err.getvalue(), "")
            self.assertLessEqual(total, cap + 4096)
            self.assertTrue(os.path.exists(paths.log_file() + ".1"))
            self.assertFalse(os.path.exists(paths.log_file() + f".{logutil.LOG_BACKUPS + 1}"))

    def test_errors_still_reach_stderr_in_daemon_mode(self):
        with temp_home():
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"LIMITPILOT_DAEMON": "1"}), \
                    contextlib.redirect_stderr(err):
                logutil.log("routine line")
                logutil.error("real problem")
            self.assertNotIn("routine line", err.getvalue())
            self.assertIn("real problem", err.getvalue())

    def test_daemon_err_log_is_rotated(self):
        with temp_home():
            os.makedirs(paths.STATE_DIR, exist_ok=True)
            path = os.path.join(paths.STATE_DIR, "daemon.err.log")
            with open(path, "w") as handle:
                handle.write("x" * (logutil.LOG_MAX_BYTES + 10))
            logutil.rotate_daemon_err_log()
            self.assertFalse(os.path.exists(path))
            self.assertTrue(os.path.exists(path + ".1"))


class LockScopeTests(unittest.TestCase):
    def test_state_lock_is_free_during_the_usage_request(self):
        with temp_home(), fake_store(paths.HOME) as store:
            _write_claude_json(1)
            store.write(paths.ACTIVE_SERVICE, paths.KC_USER, blob("token-1"))
            st = _state_with_slots(1, None)
            st["slots"]["1"]["usageFetchedAt"] = None
            state.save_state(st)
            seen = []

            def during():
                seen.append(locks.state_lock_is_free())

            src = FakeUsage({"token-1": usage(session=40, weekly=10)}, delay=0.3, during=during)
            others = []

            def other_writer():
                time.sleep(0.1)                                  # 조회가 진행 중일 때
                start = time.monotonic()
                with locks.state_lock(timeout=5):
                    others.append(time.monotonic() - start)

            with usage_source(src), mock.patch("limitpilot.logutil.log"):
                t = threading.Thread(target=other_writer)
                t.start()
                tick.run_tick()
                t.join()
            self.assertEqual(src.calls, 1)
            self.assertEqual(seen, [True])
            self.assertLess(others[0], 0.2)                     # 다른 프로세스가 기다리지 않는다
            self.assertEqual(state.load_state()["slots"]["1"]["usage"]["five_hour"]["pct"], 40.0)


class BenchScriptTests(unittest.TestCase):
    def test_bench_prints_before_after_table(self):
        env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), PYTHONDONTWRITEBYTECODE="1")
        out = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "scripts", "bench.py"),
                              "--quick"], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("| measure | before (baseline design) | after |", out.stdout)


if __name__ == "__main__":
    unittest.main()
