"""AC-5: the dashboard API needs the per-run token, a local Host and a matching Origin."""

import http.client
import json
import os
import signal
import threading
import unittest
from unittest import mock

from limitpilot.dashboard import runtime
from limitpilot.dashboard import server
from tests.support import temp_home

TOKEN = "per-run-test-token-value"


class DashboardSecurityTests(unittest.TestCase):
    def setUp(self):
        self._home = temp_home()
        self._home.__enter__()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.httpd.api_token = TOKEN
        self.httpd.shutdown_nonce = "n" * 32
        self.httpd.instance_id = "i" * 24
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self._home.__exit__(None, None, None)

    def request(self, method, path, body=None, token=TOKEN, host=None, origin=None, header=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token is not None:
            headers["X-LimitPilot-Token"] = token
        if origin is not None:
            headers["Origin"] = origin
        if header:
            headers["X-LimitPilot"] = "1"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        return resp.status, payload

    def test_status_without_token_is_forbidden(self):
        self.assertEqual(self.request("GET", "/api/status", token=None)[0], 403)

    def test_delete_without_token_is_forbidden(self):
        self.assertEqual(self.request("POST", "/api/slot/1/delete", token=None)[0], 403)

    def test_wrong_token_is_forbidden(self):
        self.assertEqual(self.request("GET", "/api/status", token="guess")[0], 403)

    def test_wrong_origin_is_forbidden(self):
        evil = "http://attacker.example"
        self.assertEqual(self.request("GET", "/api/status", origin=evil)[0], 403)
        self.assertEqual(self.request("POST", "/api/settings", {"threshold": 80}, origin=evil)[0], 403)

    def test_wrong_host_is_forbidden(self):
        # DNS rebinding: 브라우저가 공격자 도메인 이름을 Host 로 보낸다
        self.assertEqual(self.request("GET", "/api/status", host=f"evil.example:{self.port}")[0], 403)
        self.assertEqual(self.request("GET", "/", host=f"evil.example:{self.port}")[0], 403)

    def test_valid_token_and_origin_are_allowed(self):
        origin = f"http://127.0.0.1:{self.port}"
        status, body = self.request("GET", "/api/status", origin=origin)
        self.assertEqual(status, 200, body)
        self.assertIn("slots", json.loads(body))
        status, body = self.request("POST", "/api/settings", {"threshold": 85}, origin=origin)
        self.assertEqual(status, 200, body)

    def test_status_etag_returns_304_when_nothing_changed(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/status", headers={"Host": f"127.0.0.1:{self.port}",
                                                    "X-LimitPilot-Token": TOKEN})
        first = conn.getresponse()
        first.read()
        etag = first.getheader("ETag")
        self.assertTrue(etag)
        with mock.patch.object(server.status_mod, "build_status",
                               side_effect=AssertionError("must not rebuild")):
            conn.request("GET", "/api/status", headers={"Host": f"127.0.0.1:{self.port}",
                                                        "X-LimitPilot-Token": TOKEN,
                                                        "If-None-Match": etag})
            second = conn.getresponse()
            second.read()
        conn.close()
        self.assertEqual(second.status, 304)

    def test_page_carries_the_token_and_health_needs_none(self):
        status, page = self.request("GET", "/", token=None, header=False)
        self.assertEqual(status, 200)
        self.assertIn(TOKEN.encode(), page)
        self.assertNotIn(b"__DASHBOARD_TOKEN__", page)
        status, body = self.request("GET", "/api/health", token=None, header=False)
        self.assertEqual(status, 200)
        self.assertNotIn(TOKEN.encode(), body)

    def test_presence_stream_accepts_token_only_in_query(self):
        self.assertEqual(self.request("GET", "/api/presence?build=x", token=None)[0], 403)

    def test_shutdown_needs_the_nonce_not_the_token(self):
        self.assertEqual(self.request("POST", "/api/shutdown", {"nonce": "wrong"}, token=None)[0], 403)


class ProcessSafetyTests(unittest.TestCase):
    def test_unrecorded_listener_is_never_killed(self):
        with (
            mock.patch.object(runtime, "_dashboard_runtime_records", return_value=[]),
            mock.patch.object(runtime, "_dashboard_process_pids", return_value=[4242]),
            mock.patch.object(server, "_port_settled", return_value=False),
            mock.patch.object(server, "_listening_pids", return_value={4242}),
            mock.patch.object(os, "kill") as kill,
        ):
            self.assertFalse(server.stop_stale_dashboard(8748, {"instance": "other"}))
        kill.assert_not_called()

    def test_record_with_other_instance_is_not_trusted(self):
        records = [("/x", 4242, 8748, "n" * 30, "instance-a" * 2)]
        with (
            mock.patch.object(runtime, "_dashboard_runtime_records", return_value=records),
            mock.patch.object(server, "_port_settled", return_value=False),
            mock.patch.object(server, "_listening_pids", return_value={4242}),
            mock.patch.object(os, "kill") as kill,
        ):
            self.assertFalse(server.stop_stale_dashboard(8748, {"instance": "instance-b" * 2}))
        kill.assert_not_called()

    def test_purge_pattern_match_only_warns(self):
        with (
            mock.patch.object(runtime, "_dashboard_runtime_records", return_value=[]),
            mock.patch.object(runtime, "_dashboard_health", return_value=None),
            mock.patch.object(runtime, "_dashboard_process_pids", return_value=[4242]),
            mock.patch.object(runtime.time, "monotonic", side_effect=[0.0, 10.0]),
            mock.patch.object(os, "kill") as kill,
            mock.patch("sys.stderr") as err,
        ):
            runtime._stop_running_dashboards()
        kill.assert_not_called()
        self.assertTrue(err.write.called)
        self.assertNotIn(signal.SIGTERM, [c.args[-1] for c in kill.call_args_list])


if __name__ == "__main__":
    unittest.main()
