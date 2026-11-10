import contextlib
import io
import json
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
import zipfile
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from limitpilot import accounts
from limitpilot import cli
from limitpilot import collector
from limitpilot import constants
from limitpilot import errors
from limitpilot import gateway
from limitpilot import gateway_usage
from limitpilot import locks
from limitpilot import logutil
from limitpilot import notify
from limitpilot import oauth as oauth_mod
from limitpilot import paths
from limitpilot import state as state_mod
from limitpilot import switch as switch_mod
from limitpilot import tick
from limitpilot import usage_api
from limitpilot.daemon import deploy
from limitpilot.daemon import launchd
from limitpilot.dashboard import server
from limitpilot.dashboard import status
from limitpilot.login import conpty_win
from limitpilot.login import session as session_mod
from limitpilot.policy import decide
from limitpilot.policy import limits
from limitpilot.store import creds
from limitpilot.store import filestore
from limitpilot.store import keychain


class CurrentUsageResponseTests(unittest.TestCase):
    def test_limits_are_preferred_and_missing_window_uses_legacy_fallback(self):
        usage = usage_api.normalize_usage({
            "limits": [
                {"kind": "session", "percent": 91,
                 "resets_at": "2026-07-10T12:00:00Z"},
                {"kind": "weekly_scoped", "percent": 99,
                 "resets_at": "2026-07-11T12:00:00Z"},
            ],
            "five_hour": {"utilization": 12, "resets_at": "legacy-session"},
            "seven_day": {"utilization": 33, "resets_at": "legacy-weekly"},
        })

        self.assertEqual(usage["five_hour"], {
            "pct": 91.0, "resets_at": "2026-07-10T12:00:00Z",
        })
        self.assertEqual(usage["seven_day"], {
            "pct": 33.0, "resets_at": "legacy-weekly",
        })

    def test_limits_threshold_drives_the_same_switch_decision_as_dashboard(self):
        state = state_mod.default_state()
        state["activeSlot"] = 1
        state["settings"] = {"threshold": 90}
        state["slots"] = {
            "1": {"enabled": True},
            "2": {"enabled": True},
        }
        usages = {
            1: usage_api.normalize_usage({
                "limits": [
                    {"kind": "session", "percent": 90,
                     "resets_at": "2026-07-10T12:00:00Z"},
                    {"kind": "weekly_all", "percent": 20,
                     "resets_at": "2026-07-11T12:00:00Z"},
                ],
            }),
            2: usage_api.normalize_usage({
                "limits": [
                    {"kind": "session", "percent": 10,
                     "resets_at": "2026-07-10T13:00:00Z"},
                    {"kind": "weekly_all", "percent": 30,
                     "resets_at": "2026-07-12T12:00:00Z"},
                ],
            }),
        }

        action, target, reason = decide.decide(state, usages)

        self.assertEqual((action, target), ("switch", 2))
        self.assertIn("활성 90%", reason)


class StableDaemonInstallationTests(unittest.TestCase):
    def test_deployed_copy_is_private_zipapp_independent_of_install_location(self):
        with tempfile.TemporaryDirectory() as root:
            state_dir = os.path.join(root, "state")

            with mock.patch.object(paths, "STATE_DIR", state_dir):
                target = deploy.deploy_daemon_script()

                self.assertEqual(target, deploy.deployed_daemon_script_path())
                self.assertTrue(target.endswith("limitpilot.pyz"))
                self.assertTrue(zipfile.is_zipfile(target))
                with zipfile.ZipFile(target) as zf:
                    names = set(zf.namelist())
                self.assertIn("__main__.py", names)
                self.assertIn("limitpilot/cli.py", names)
                self.assertIn("limitpilot/dashboard/static/dashboard.html", names)
                self.assertFalse(any("__pycache__" in n for n in names))
                self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(target)).st_mode),
                                 0o700)

    def test_pyz_build_is_deterministic(self):
        self.assertEqual(deploy.build_pyz_bytes(), deploy.build_pyz_bytes())

    def test_install_plist_uses_stable_copy_without_calling_launchctl(self):
        with tempfile.TemporaryDirectory() as root:
            state_dir = os.path.join(root, "state")
            launch_agent = os.path.join(root, "LaunchAgents", "limitpilot.plist")
            completed = mock.Mock(returncode=0, stderr="")

            with (
                mock.patch.object(paths, "STATE_DIR", state_dir),
                mock.patch.object(paths, "LAUNCHD_PLIST", launch_agent),
                mock.patch.object(deploy, "find_python", return_value="/usr/bin/python3"),
                mock.patch.object(subprocess, "run", return_value=completed) as run,
                mock.patch.object(logutil, "log"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli.cmd_install_daemon(None), constants.EXIT_SWITCHED)
                with open(launch_agent, "rb") as f:
                    plist = plistlib.load(f)
                target = deploy.deployed_daemon_script_path()

            self.assertEqual(plist["ProgramArguments"], [
                "/usr/bin/python3", target, "auto", "--once",
            ])
            self.assertEqual(plist["Label"], "io.github.limitpilot.daemon")
            self.assertEqual(plist["EnvironmentVariables"], {"LIMITPILOT_DAEMON": "1"})
            self.assertEqual(plist["StandardOutPath"], "/dev/null")
            self.assertEqual(plist["StandardErrorPath"],
                             os.path.join(state_dir, "daemon.err.log"))
            self.assertTrue(zipfile.is_zipfile(target))
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[1].args[0][0:2],
                             ["launchctl", "bootstrap"])

    def test_plist_escapes_special_characters_in_paths(self):
        with mock.patch.object(paths, "STATE_DIR", "/tmp/a&b<c>"):
            data = launchd.build_plist("/opt/py & co/python3", "/tmp/a&b<c>/limitpilot.pyz")
        plist = plistlib.loads(data)
        self.assertEqual(plist["ProgramArguments"][0], "/opt/py & co/python3")
        self.assertIn(b"&amp;", data)

    def test_uninstall_removes_deployed_copy_and_verifies_launchctl(self):
        with tempfile.TemporaryDirectory() as root:
            state_dir = os.path.join(root, "state")
            launch_agent = os.path.join(root, "LaunchAgents", "limitpilot.plist")
            fallback = os.path.join(state_dir, paths.LAUNCHD_LABEL + ".plist")
            with mock.patch.object(paths, "STATE_DIR", state_dir):
                deployed = deploy.deployed_daemon_script_path()
            for path in (launch_agent, fallback, deployed):
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_text("temporary", encoding="utf-8")

            with (
                mock.patch.object(paths, "STATE_DIR", state_dir),
                mock.patch.object(paths, "LAUNCHD_PLIST", launch_agent),
                mock.patch.object(subprocess, "run", side_effect=[
                    mock.Mock(returncode=0),    # print: loaded before removal
                    mock.Mock(returncode=0),    # bootout
                    mock.Mock(returncode=0),    # unload primary plist
                    mock.Mock(returncode=0),    # unload fallback plist
                    mock.Mock(returncode=constants.LAUNCHCTL_NOT_FOUND_RC),  # exact service absent
                ]) as run,
                mock.patch.object(locks, "state_lock", return_value=nullcontext()),
                mock.patch.object(logutil, "log"),
            ):
                self.assertEqual(cli.cmd_uninstall_daemon(None), constants.EXIT_SWITCHED)

            self.assertFalse(os.path.exists(deployed))
            self.assertFalse(os.path.exists(launch_agent))
            self.assertFalse(os.path.exists(fallback))
            self.assertEqual(run.call_count, 5)
            self.assertEqual(run.call_args_list[-1].args[0][:2],
                             ["launchctl", "print"])


class ManualActivationTests(unittest.TestCase):
    def test_dashboard_activation_delegates_to_safe_switch_with_force_flag(self):
        state = state_mod.default_state()
        state["slots"]["2"] = {
            "email": "target@example.com",
            "alias": "Target",
            "oauthAccount": {"emailAddress": "target@example.com"},
            "enabled": True,
            "kind": "oauth",
        }

        with (
            mock.patch.object(locks, "state_lock", return_value=nullcontext()),
            mock.patch.object(state_mod, "load_state", return_value=state),
            mock.patch.object(switch_mod, "perform_switch", return_value="switched") as switch,
        ):
            message = server.mutate_slot(2, "activate", force=True)

        switch.assert_called_once_with(state, 2, force=True)
        self.assertIn("Target", message)
        self.assertIn("활성화 완료", message)


def _usage(session_pct, weekly_pct=10):
    return usage_api.normalize_usage({
        "limits": [
            {"kind": "session", "percent": session_pct,
             "resets_at": "2099-07-10T12:00:00Z"},
            {"kind": "weekly_all", "percent": weekly_pct,
             "resets_at": "2099-07-11T12:00:00Z"},
        ],
    })


def _ent_usage(used=0.02, limit=500.0):
    """엔터프라이즈 정규화 usage — 세션/주간 창 없이 spend(달러 한도)만."""
    return usage_api.normalize_usage({
        "five_hour": None, "seven_day": None, "limits": [],
        "spend": {
            "used": {"amount_minor": round(used * 100), "exponent": 2, "currency": "USD"},
            "limit": {"amount_minor": round(limit * 100), "exponent": 2, "currency": "USD"},
            "enabled": True,
        },
    })


ENTERPRISE_OAUTH = {"organizationType": "claude_enterprise",
                    "accountUuid": "ent-a", "organizationUuid": "ent-o"}


def _cold_usage(weekly_pct=10):
    """미사용/리셋 직후 계정 — 세션 창 미시작(0%·resets_at 없음)."""
    return usage_api.normalize_usage({
        "limits": [
            {"kind": "session", "percent": 0, "resets_at": None},
            {"kind": "weekly_all", "percent": weekly_pct,
             "resets_at": "2099-07-11T12:00:00Z"},
        ],
    })


def _running_usage(session_pct, resets_in_s, weekly_pct=10):
    """세션 창이 이미 돌고 있는 계정 — resets_in_s초 뒤 리셋."""
    resets_at = (datetime.now(timezone.utc) + timedelta(seconds=resets_in_s)
                 ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return usage_api.normalize_usage({
        "limits": [
            {"kind": "session", "percent": session_pct, "resets_at": resets_at},
            {"kind": "weekly_all", "percent": weekly_pct,
             "resets_at": "2099-07-11T12:00:00Z"},
        ],
    })


class ActiveSlotMeasurementRegressionTests(unittest.TestCase):
    """활성 슬롯이 OFF/격리여도(force 전환으로 도달 가능) 실측되어야 하며,
    None으로 보고해 거짓 failover를 유발하면 안 된다."""

    def _collect_with_active_slot(self, slot_extra):
        st = state_mod.default_state()
        st["activeSlot"] = 1
        st["slots"] = {"1": {"email": "a@example.com", "enabled": True,
                             "oauthAccount": {"emailAddress": "a@example.com"},
                             **slot_extra}}
        norm = _usage(5)
        with (
            mock.patch.object(collector, "maybe_release_quarantine"),
            mock.patch.object(filestore, "read_json",
                              return_value={"oauthAccount": {"emailAddress": "a@example.com"}}),
            mock.patch.object(accounts, "identity_uuid_pair", return_value=True),
            mock.patch.object(accounts, "find_slot_by_uuid", return_value=1),
            mock.patch.object(creds, "kc_read_active", return_value="blob"),
            mock.patch.object(oauth_mod, "parse_oauth", return_value={"accessToken": "tok"}),
            mock.patch.object(oauth_mod, "is_expired", return_value=False),
            mock.patch.object(usage_api, "fetch_usage", return_value=(norm, None, None)),
        ):
            result = collector.collect_usage(st)
        return result, norm

    def test_disabled_active_slot_is_still_measured(self):
        result, norm = self._collect_with_active_slot({"enabled": False})
        self.assertEqual(result[1], norm)

    def test_quarantined_active_slot_is_still_measured(self):
        result, norm = self._collect_with_active_slot(
            {"quarantine": {"reason": "invalid_grant"}})
        self.assertEqual(result[1], norm)


class LanguageSettingTests(unittest.TestCase):
    def test_default_language_is_korean(self):
        self.assertEqual(state_mod.get_settings(state_mod.default_state())["language"], "ko")

    def test_set_language_normalizes_validates_and_persists(self):
        st = state_mod.default_state()
        with mock.patch.object(state_mod, "save_state"):
            s = state_mod.set_setting(st, "language", " EN ")
            self.assertEqual(s["language"], "en")
            self.assertEqual(state_mod.get_settings(st)["language"], "en")
            with self.assertRaises(errors.LimitPilotError):
                state_mod.set_setting(st, "language", "jp")
        # 실패한 설정은 저장되지 않아야 한다
        self.assertEqual(state_mod.get_settings(st)["language"], "en")

    def test_set_setting_preserves_unknown_keys(self):
        # 알 수 없는(수동 추가) 키는 config 쓰기에 지워지면 안 된다
        st = state_mod.default_state()
        st["settings"] = {"threshold": 85, "customKey": "keep-me"}
        with mock.patch.object(state_mod, "save_state"):
            state_mod.set_setting(st, "language", "en")
        self.assertEqual(st["settings"].get("customKey"), "keep-me")
        self.assertEqual(st["settings"]["language"], "en")

    def test_set_setting_rejects_unknown_key(self):
        st = state_mod.default_state()
        with mock.patch.object(state_mod, "save_state"):
            with self.assertRaises(errors.LimitPilotError):
                state_mod.set_setting(st, "refreshSec", 30)  # 제거된 설정 — 거부


class UsageFetchBudgetTests(unittest.TestCase):
    """usage API의 비공개 호출 제한(429 페널티 윈도우) 대응 —
    슬롯별 지수 백오프(Retry-After 우선), 비활성 슬롯(OFF 포함) 5분 주기 실측,
    활성이 임계 근접/측정 불가면 후보 즉시 승격."""

    def _state(self, active_extra=None, cand_extra=None):
        st = state_mod.default_state()
        st["activeSlot"] = 1
        st["slots"] = {"1": {"email": "a@example.com", "enabled": True,
                             "oauthAccount": {"emailAddress": "a@example.com"},
                             **(active_extra or {})}}
        if cand_extra is not None:
            st["slots"]["2"] = {"email": "b@example.com", "enabled": True,
                                "oauthAccount": {"emailAddress": "b@example.com"},
                                **cand_extra}
        return st

    def _collect(self, st, fetch_returns, expired_active=False):
        # 활성 슬롯은 is_expired(oauth) 1-인자, 후보는 (oauth, buffer) 2-인자 호출
        exp = (lambda *a: len(a) == 1) if expired_active else (lambda *a: False)
        with (
            mock.patch.object(logutil, "log"),  # 실제 limitpilot.log 오염 방지
            mock.patch.object(collector, "maybe_release_quarantine"),
            mock.patch.object(filestore, "read_json",
                              return_value={"oauthAccount": {"emailAddress": "a@example.com"}}),
            mock.patch.object(accounts, "identity_uuid_pair", return_value=True),
            mock.patch.object(accounts, "find_slot_by_uuid", return_value=1),
            mock.patch.object(creds, "kc_read_active", return_value="blob"),
            mock.patch.object(creds, "kc_read_slot", return_value="blob"),
            mock.patch.object(oauth_mod, "parse_oauth", return_value={"accessToken": "tok"}),
            mock.patch.object(oauth_mod, "is_expired", side_effect=exp),
            mock.patch.object(usage_api, "fetch_usage", side_effect=fetch_returns) as fetch,
        ):
            result = collector.collect_usage(st)
        return result, fetch

    def test_429_sets_backoff_and_skips_until_expiry(self):
        st = self._state()
        self._collect(st, [(None, "http-429", None)])
        bo = st["slots"]["1"]["fetchBackoff"]
        self.assertEqual(bo["fails"], 1)
        self.assertAlmostEqual(bo["until"] - time.time(),
                               constants.FETCH_BACKOFF_BASE_S, delta=5)

        _, fetch = self._collect(st, [])
        fetch.assert_not_called()  # 백오프 중 재타격 금지

    def test_active_backoff_is_capped_for_decision_freshness(self):
        # 활성은 백오프가 길어지면 스냅샷이 stale(300s)로 굳어 거짓 failover —
        # fails 누적·Retry-After와 무관하게 BASE(120s)로 상한
        st = self._state(active_extra={
            "fetchBackoff": {"fails": 3, "until": time.time() - 1}})
        self._collect(st, [(None, "http-429", 600.0)])
        bo = st["slots"]["1"]["fetchBackoff"]
        self.assertEqual(bo["fails"], 4)
        self.assertAlmostEqual(bo["until"] - time.time(),
                               constants.FETCH_BACKOFF_BASE_S, delta=5)

    def test_candidate_backoff_grows_and_honors_retry_after(self):
        st = self._state(cand_extra={"usage": _usage(10),
                                     "usageFetchedAt": time.time() - 700})
        # 활성 성공 + 후보 429(Retry-After 600) → 후보 백오프 600s
        self._collect(st, [(_usage(5), None, None), (None, "http-429", 600.0)])
        bo = st["slots"]["2"]["fetchBackoff"]
        self.assertEqual(bo["fails"], 1)
        self.assertAlmostEqual(bo["until"] - time.time(), 600.0, delta=5)

        # 만료 후 재차 429(헤더 없음) → 지수 스케줄 2배
        # (활성은 방금 성공으로 신선 — 이번 틱은 후보만 실측된다)
        bo["until"] = time.time() - 1
        self._collect(st, [(None, "http-429", None)])
        bo = st["slots"]["2"]["fetchBackoff"]
        self.assertEqual(bo["fails"], 2)
        self.assertAlmostEqual(bo["until"] - time.time(),
                               constants.FETCH_BACKOFF_BASE_S * 2, delta=5)

    def test_success_resets_backoff(self):
        st = self._state(active_extra={
            "fetchBackoff": {"fails": 3, "until": time.time() - 1}})
        result, _ = self._collect(st, [(_usage(5), None, None)])
        self.assertIsNone(st["slots"]["1"]["fetchBackoff"])
        self.assertEqual(result[1], _usage(5))

    def test_fresh_candidate_not_probed_when_active_low(self):
        st = self._state(cand_extra={"usage": _usage(10),
                                     "usageFetchedAt": time.time() - 60})
        result, fetch = self._collect(st, [(_usage(5), None, None)])
        self.assertEqual(fetch.call_count, 1)  # 활성만 실측
        self.assertEqual(result[2], st["slots"]["2"]["usage"])

    def test_candidate_promoted_when_active_near_threshold(self):
        st = self._state(cand_extra={"usage": _usage(10),
                                     "usageFetchedAt": time.time() - 60})
        _, fetch = self._collect(st, [(_usage(80), None, None),
                                      (_usage(10), None, None)])
        self.assertEqual(fetch.call_count, 2)  # 임계-15% 이상 — 후보 승격

    def test_stale_candidate_probed_on_cadence(self):
        st = self._state(cand_extra={"usage": _usage(10),
                                     "usageFetchedAt": time.time() - 700})
        _, fetch = self._collect(st, [(_usage(5), None, None),
                                      (_usage(12), None, None)])
        self.assertEqual(fetch.call_count, 2)  # 5분 경과 — 정기 실측

    def test_disabled_slot_refreshed_for_display_but_never_promoted(self):
        st = self._state(cand_extra={"enabled": False, "usage": _usage(10),
                                     "usageFetchedAt": time.time() - 700})
        _, fetch = self._collect(st, [(_usage(80), None, None),
                                      (_usage(12), None, None)])
        self.assertEqual(fetch.call_count, 2)  # OFF여도 5분 지나면 표시용 실측

        st["slots"]["2"]["usageFetchedAt"] = time.time() - 60
        _, fetch = self._collect(st, [(_usage(80), None, None)])
        self.assertEqual(fetch.call_count, 1)  # 신선하면 위급 승격 대상 아님

    def test_token_expired_active_does_not_promote_candidates(self):
        # decide가 관망하는 상태(활성 토큰 만료)에서는 후보 실측을 서두르지 않는다
        st = self._state(cand_extra={"usage": _usage(10),
                                     "usageFetchedAt": time.time() - 60})
        result, fetch = self._collect(st, [], expired_active=True)
        fetch.assert_not_called()
        self.assertEqual(result[1], constants.TOKEN_EXPIRED)

    def test_active_probe_skips_when_fresh_and_calm(self):
        # 활성도 정기 주기(ACTIVE_PROBE_S)로만 실측 — 신선+임계 여유면 스킵
        st = self._state(active_extra={"usage": _usage(10),
                                       "usageFetchedAt": time.time() - 60})
        result, fetch = self._collect(st, [])
        fetch.assert_not_called()
        self.assertEqual(result[1], st["slots"]["1"]["usage"])

    def test_active_probed_every_tick_near_threshold(self):
        st = self._state(active_extra={"usage": _usage(80),
                                       "usageFetchedAt": time.time() - 60})
        _, fetch = self._collect(st, [(_usage(81), None, None)])
        fetch.assert_called_once()  # 임계-15% 이상 — 매 틱 실측

    def test_active_probed_on_cadence_when_stale(self):
        st = self._state(active_extra={"usage": _usage(10),
                                       "usageFetchedAt": time.time() - 200})
        _, fetch = self._collect(st, [(_usage(11), None, None)])
        fetch.assert_called_once()  # ACTIVE_PROBE_S(180s) 경과 — 정기 실측

    def test_zero_usage_candidate_still_respects_cadence(self):
        # resets_at 없는 0% 창(신규/리셋 직후 계정)도 실측 신선도로 게이트 —
        # 스냅샷 신뢰 여부(stale_or_none)와 무관하게 매 틱 재실측 금지(429 예산)
        u = usage_api.normalize_usage(
            {"limits": [{"kind": "session", "percent": 0, "resets_at": None}]})
        st = self._state(cand_extra={"usage": u,
                                     "usageFetchedAt": time.time() - 60})
        _, fetch = self._collect(st, [(_usage(5), None, None)])
        self.assertEqual(fetch.call_count, 1)  # 활성만 실측

    def test_fresh_cold_candidate_snapshot_reaches_decision(self):
        # 실측 사이 틱(신선 캐시 경로)에서 미시작 창(0%·resets_at 없음) 스냅샷이
        # '사용량 불명'으로 강등되면 여유 100% 후보가 선제 전환·failover에서 빠진다
        u = _cold_usage()
        st = self._state(cand_extra={"usage": u,
                                     "usageFetchedAt": time.time() - 60})
        result, fetch = self._collect(st, [(_usage(5), None, None)])
        self.assertEqual(fetch.call_count, 1)  # 활성만 실측
        self.assertEqual(result[2], u)

    def test_enterprise_active_respects_cadence_via_spend(self):
        # 엔터프라이즈 활성은 세션 % 대신 spend 소진율(낮음)로 케이던스 판단 — 신선하면
        # 정기 주기까지 스킵(binding_pct가 None이라고 매 틱 실측하면 429 폭주)
        st = self._state(active_extra={
            "usage": _ent_usage(), "usageFetchedAt": time.time() - 60,
            "oauthAccount": ENTERPRISE_OAUTH})
        result, fetch = self._collect(st, [])
        fetch.assert_not_called()
        self.assertEqual(result[1], st["slots"]["1"]["usage"])

    def test_enterprise_active_does_not_promote_candidates(self):
        # 엔터프라이즈 활성은 전환 결정이 없어 후보를 급행 실측(probe_all)하지 않는다
        st = self._state(
            active_extra={"usage": _ent_usage(), "usageFetchedAt": time.time() - 60,
                          "oauthAccount": ENTERPRISE_OAUTH},
            cand_extra={"usage": _usage(10), "usageFetchedAt": time.time() - 60})
        _, fetch = self._collect(st, [])
        fetch.assert_not_called()


class FailoverThrashingRegressionTests(unittest.TestCase):
    """측정 불가(failover)는 미지 후보로 전환하지 않고 관망해야 하며(핑퐁 방지),
    전환 결정 시 unhealthyTicks는 새 활성으로 승계되지 않아야 한다."""

    def _state(self):
        st = state_mod.default_state()
        st["activeSlot"] = 1
        st["settings"] = {"threshold": 90}
        st["slots"] = {"1": {"enabled": True}, "2": {"enabled": True}}
        return st

    def test_unmeasurable_active_with_unknown_candidates_holds(self):
        st = self._state()
        st["unhealthyTicks"] = constants.UNHEALTHY_TICKS - 1
        action, target, reason = decide.decide(st, {1: None, 2: None})
        self.assertEqual(action, "blocked")
        self.assertIsNone(target)
        self.assertIn("관망", reason)

    def test_at_limit_still_switches_to_unknown_candidate(self):
        st = self._state()
        action, target, _ = decide.decide(st, {1: _usage(100), 2: None})
        self.assertEqual((action, target), ("switch", 2))
        self.assertEqual(st["unhealthyTicks"], 0)

    def test_failover_to_measured_candidate_resets_unhealthy_ticks(self):
        st = self._state()
        st["unhealthyTicks"] = constants.UNHEALTHY_TICKS - 1
        action, target, _ = decide.decide(st, {1: None, 2: _usage(10)})
        self.assertEqual((action, target), ("switch", 2))
        self.assertEqual(st["unhealthyTicks"], 0)


class ColdSnapshotTrustTests(unittest.TestCase):
    """0% 창의 신선한 스냅샷은 결정에 신뢰한다 — 리셋을 거쳐도 0%라 과대평가가
    불가능하기 때문. 사용 중(pct>0)인데 resets_at이 없거나 리셋이 지난/깨진
    창과 STALE_OK_S를 넘긴 스냅샷은 종전대로 버린다."""

    def test_fresh_cold_snapshot_is_trusted(self):
        u = _cold_usage()
        s = {"usage": u, "usageFetchedAt": time.time() - 60}
        self.assertEqual(limits.stale_or_none(s), u)

    def test_zero_pct_with_past_resets_at_is_trusted(self):
        # 리셋이 지난 0% 창 — 리셋 후에도 0%이므로 여전히 신뢰
        u = _running_usage(0, -3600)
        s = {"usage": u, "usageFetchedAt": time.time() - 60}
        self.assertEqual(limits.stale_or_none(s), u)

    def test_nonzero_pct_without_resets_at_is_still_discarded(self):
        u = usage_api.normalize_usage(
            {"limits": [{"kind": "session", "percent": 42, "resets_at": None}]})
        s = {"usage": u, "usageFetchedAt": time.time() - 60}
        self.assertIsNone(limits.stale_or_none(s))

    def test_nonzero_pct_with_past_resets_at_is_still_discarded(self):
        s = {"usage": _running_usage(42, -3600),
             "usageFetchedAt": time.time() - 60}
        self.assertIsNone(limits.stale_or_none(s))

    def test_old_cold_snapshot_is_still_discarded(self):
        s = {"usage": _cold_usage(),
             "usageFetchedAt": time.time() - constants.STALE_OK_S - 10}
        self.assertIsNone(limits.stale_or_none(s))

    def test_malformed_resets_at_is_discarded_without_crash(self):
        # state.json에 뭐가 들어 있든 틱을 죽이면 안 된다 — 비문자열 resets_at
        u = {"five_hour": {"pct": 42.0, "resets_at": 123},
             "seven_day": None, "windows": [], "spend": None}
        s = {"usage": u, "usageFetchedAt": time.time() - 60}
        self.assertIsNone(limits.stale_or_none(s))

    def test_session_reset_epoch_survives_malformed_resets_at(self):
        now = time.time()
        u = {"five_hour": {"pct": 10.0, "resets_at": 123}}
        self.assertEqual(limits.session_reset_epoch(u, now),
                         now + constants.SESSION_WINDOW_S)


class ResetAwareTieBreakTests(unittest.TestCase):
    """후보 선택은 candidate_score(구속 사용률 − 세션 리셋 보너스) 최저 순 —
    잔여량이 사실상 같다면 5시간 창 리셋이 빨리 돌아오는 쪽을 고르되, 보너스는
    리셋 임박도에 비례하고 주간 창 여유로 캡된다."""

    def _state(self):
        st = state_mod.default_state()
        st["activeSlot"] = 1
        st["settings"] = {"threshold": 90}
        st["slots"] = {str(i): {"enabled": True} for i in (1, 2, 3)}
        return st

    def test_at_limit_prefers_candidate_whose_window_resets_sooner(self):
        # 슬롯 번호 순이었다면 2(cold)가 뽑힐 자리 — 창이 이미 1시간 뒤 리셋으로
        # 돌고 있는 3이 이겨야 한다
        st = self._state()
        action, target, _ = decide.decide(st, {
            1: _usage(100), 2: _cold_usage(), 3: _running_usage(0, 3600)})
        self.assertEqual((action, target), ("switch", 3))

    def test_usage_gap_beyond_bonus_cap_still_wins_over_earlier_reset(self):
        # 리셋 보너스 상한(RESET_TIE_MAX_PCT)을 넘는 사용률 차이는 못 뒤집는다
        st = self._state()
        action, target, _ = decide.decide(st, {
            1: _usage(100), 2: _cold_usage(), 3: _running_usage(35, 3600)})
        self.assertEqual((action, target), ("switch", 2))

    def test_nearly_exhausted_candidate_never_beats_healthier_one(self):
        # 회귀: 사용률 상단에서 '동급' 처리로 잔여 0.1% 후보가 뽑히면 안 된다 —
        # 리셋이 멀수록 보너스가 작아 여유 있는 후보(95%)가 이겨야 한다
        st = self._state()
        action, target, _ = decide.decide(st, {
            1: _usage(100),
            2: _running_usage(95, 17400),    # 잔여 5%, 리셋 4시간 50분 뒤
            3: _running_usage(99.9, 14400)})  # 잔여 0.1%, 리셋 4시간 뒤
        self.assertEqual((action, target), ("switch", 2))

    def test_weekly_bound_candidate_gets_no_session_reset_credit(self):
        # 주간 창이 구속인 후보의 세션 리셋은 주간을 풀어주지 못한다 —
        # 보너스가 주간 여유(0.1%)로 캡돼 여유 있는 후보(주간 95%)가 이긴다
        st = self._state()
        action, target, _ = decide.decide(st, {
            1: _usage(100),
            2: _cold_usage(weekly_pct=95),
            3: _running_usage(20, 300, weekly_pct=99.9)})
        self.assertEqual((action, target), ("switch", 2))

    def test_past_resets_at_counts_as_window_not_running(self):
        # 리셋이 지난 창은 미시작과 동급(보너스 0) — 돌고 있는 창이 이긴다
        st = self._state()
        action, target, _ = decide.decide(st, {
            1: _usage(100),
            2: _running_usage(0, -3600), 3: _running_usage(0, 3600)})
        self.assertEqual((action, target), ("switch", 3))

    def test_proactive_switch_uses_reset_tiebreak(self):
        st = self._state()
        action, target, reason = decide.decide(st, {
            1: _usage(91), 2: _cold_usage(), 3: _running_usage(2, 1800)})
        self.assertEqual((action, target), ("switch", 3))
        self.assertIn("선제 전환", reason)

    def test_all_cold_candidates_keep_slot_order(self):
        # 리셋 정보가 없는 동급 후보끼리는 기존 동작(사용률→슬롯 번호) 유지
        st = self._state()
        action, target, _ = decide.decide(st, {
            1: _usage(100), 2: _cold_usage(), 3: _cold_usage()})
        self.assertEqual((action, target), ("switch", 2))

    def test_best_candidate_applies_same_tiebreak(self):
        st = self._state()
        st["slots"]["2"]["usage"] = _cold_usage()
        st["slots"]["3"]["usage"] = _running_usage(1, 3600)
        self.assertEqual(decide.best_candidate(st, 1), 3)

    def test_best_candidate_honors_usages_override(self):
        # usages 인자가 저장된 스냅샷보다 우선하는 분기도 같은 점수 규칙을 탄다
        st = self._state()
        st["slots"]["3"]["usage"] = _usage(50)
        usages = {2: _cold_usage(), 3: _running_usage(0, 3600)}
        self.assertEqual(decide.best_candidate(st, 1, usages), 3)


class QuarantineFingerprintBackfillTests(unittest.TestCase):
    def _quarantined_state(self, stored_fp):
        st = state_mod.default_state()
        st["slots"] = {"3": {"email": "q@example.com", "enabled": True,
                             "quarantine": {"reason": "invalid_grant",
                                            "refreshTokenFingerprint": stored_fp}}}
        return st

    def test_missing_fingerprint_is_backfilled_instead_of_released(self):
        st = self._quarantined_state(None)
        with (
            mock.patch.object(creds, "kc_read_slot", return_value="blob"),
            mock.patch.object(oauth_mod, "refresh_fingerprint", return_value="sha256:now"),
        ):
            collector.maybe_release_quarantine(st, 3)
        q = st["slots"]["3"]["quarantine"]
        self.assertIsNotNone(q)  # 오해제 금지
        self.assertEqual(q["refreshTokenFingerprint"], "sha256:now")

    def test_changed_fingerprint_still_releases(self):
        st = self._quarantined_state("sha256:old")
        with (
            mock.patch.object(creds, "kc_read_slot", return_value="blob"),
            mock.patch.object(oauth_mod, "refresh_fingerprint", return_value="sha256:new"),
            mock.patch.object(logutil, "log"),
        ):
            collector.maybe_release_quarantine(st, 3)
        self.assertIsNone(st["slots"]["3"]["quarantine"])


class RotatedTokenPreservationTests(unittest.TestCase):
    def test_write_slot_retry_survives_transient_failure(self):
        with (
            mock.patch.object(creds, "kc_write_slot",
                              side_effect=[errors.KeychainError("잠금"), None]) as w,
            mock.patch.object(time, "sleep"),
        ):
            creds.kc_write_slot_retry(2, "blob")
        self.assertEqual(w.call_count, 2)

    def test_persistent_write_failure_does_not_kill_tick_or_quarantine(self):
        st = state_mod.default_state()
        st["activeSlot"] = None
        st["slots"] = {"2": {"email": "b@example.com", "enabled": True}}
        with (
            mock.patch.object(collector, "maybe_release_quarantine"),
            mock.patch.object(creds, "kc_read_slot", return_value="old-blob"),
            mock.patch.object(oauth_mod, "parse_oauth", return_value={"accessToken": "t"}),
            mock.patch.object(oauth_mod, "is_expired", return_value=True),
            mock.patch.object(oauth_mod, "try_refresh", return_value=("new-blob", None)),
            mock.patch.object(creds, "kc_write_slot_retry",
                              side_effect=errors.KeychainError("잠금")),
            mock.patch.object(collector, "quarantine") as quarantined,
            mock.patch.object(logutil, "log"),
        ):
            result = collector.collect_usage(st)  # 예외로 틱이 죽으면 여기서 실패
        self.assertIsNone(result[2])  # stale 없음 → None, 다음 틱 재시도
        quarantined.assert_not_called()


class SwitchRollbackHardeningTests(unittest.TestCase):
    def test_readback_exception_rolls_back_active_credentials(self):
        st = state_mod.default_state()
        st["activeSlot"] = 1
        st["slots"] = {
            "1": {"email": "a@example.com", "enabled": True,
                  "oauthAccount": {"emailAddress": "a@example.com"}},
            "2": {"email": "b@example.com", "enabled": True,
                  "oauthAccount": {"emailAddress": "b@example.com"}},
        }
        writes = []
        with (
            mock.patch.object(locks, "proper_lockfile", return_value=nullcontext()),
            mock.patch.object(creds, "kc_read_active",
                              side_effect=["live-blob", errors.KeychainError("잠금"),
                                           "live-blob"]),
            mock.patch.object(accounts, "read_live_oauth_account",
                              return_value={"emailAddress": "a@example.com"}),
            mock.patch.object(accounts, "identity_uuid_pair", return_value=True),
            mock.patch.object(accounts, "find_slot_by_uuid", return_value=1),
            mock.patch.object(creds, "kc_write_slot"),
            mock.patch.object(creds, "kc_read_slot", return_value="tgt-blob"),
            mock.patch.object(creds, "kc_write_active",
                              side_effect=lambda b: writes.append(b)),
            mock.patch.object(switch_mod, "splice_oauth_account") as splice,
            mock.patch.object(state_mod, "save_state"),
            mock.patch.object(logutil, "log"),
            mock.patch.object(notify, "notify"),
        ):
            with self.assertRaisesRegex(errors.SwitchError, "검증 불가"):
                switch_mod.perform_switch(st, 2)
        self.assertEqual(writes, ["tgt-blob", "live-blob"])  # 쓰기 → 원복
        splice.assert_not_called()  # claude.json은 건드리지 않은 채 실패해야 함


class DaemonCopyAutoRefreshTests(unittest.TestCase):
    def test_stale_copy_is_refreshed_when_cli_runs_from_source(self):
        with tempfile.TemporaryDirectory() as root:
            state_dir = os.path.join(root, "state")
            with mock.patch.object(paths, "STATE_DIR", state_dir):
                deployed = Path(deploy.deployed_daemon_script_path())
                deployed.parent.mkdir(parents=True)
                deployed.write_text("# v1\n", encoding="utf-8")
                with mock.patch.object(logutil, "log"):
                    deploy.maybe_refresh_daemon_copy()
                self.assertTrue(zipfile.is_zipfile(deployed))
                self.assertEqual(deploy.deployed_fingerprint(str(deployed)),
                                 deploy.source_fingerprint())
                # 같은 코드면 다시 쓰지 않는다(지문 비교만)
                before = deployed.stat().st_mtime_ns
                with mock.patch.object(deploy, "write_pyz") as write:
                    deploy.maybe_refresh_daemon_copy()
                write.assert_not_called()
                self.assertEqual(deployed.stat().st_mtime_ns, before)

    def test_noop_when_daemon_not_installed(self):
        with tempfile.TemporaryDirectory() as root:
            state_dir = os.path.join(root, "state")
            with mock.patch.object(paths, "STATE_DIR", state_dir):
                deploy.maybe_refresh_daemon_copy()
                self.assertFalse(os.path.exists(deploy.deployed_daemon_script_path()))

    def test_noop_when_running_as_the_deployed_copy(self):
        with tempfile.TemporaryDirectory() as root:
            state_dir = os.path.join(root, "state")
            with mock.patch.object(paths, "STATE_DIR", state_dir):
                deployed = Path(deploy.deployed_daemon_script_path())
                deployed.parent.mkdir(parents=True)
                deployed.write_text("# v1\n", encoding="utf-8")
                with mock.patch.object(deploy, "running_archive", return_value=str(deployed)):
                    deploy.maybe_refresh_daemon_copy()
                self.assertEqual(deployed.read_text(encoding="utf-8"), "# v1\n")


class DaemonInterpreterTests(unittest.TestCase):
    def test_find_python_prefers_current_interpreter_over_shim(self):
        # pyenv 셔임은 launchd 최소 PATH에서 시스템 파이썬으로 풀릴 수 있음
        self.assertEqual(deploy.find_python(), sys.executable)


class WindowsBackendTests(unittest.TestCase):
    """Windows 분기(파일 자격증명 저장소·데몬·게이트) — IS_WIN 모킹으로 mac에서 검증.
    msvcrt/schtasks 등 Windows 전용 API 자체는 이 머신에서 실행 불가 — 실행 없이
    호출 구성만 검증한다."""

    def test_active_credentials_use_claude_file_on_windows(self):
        with tempfile.TemporaryDirectory() as root:
            cred = os.path.join(root, ".claude", ".credentials.json")
            with (
                mock.patch.object(paths, "IS_WIN", True),
                mock.patch.object(paths, "CC_CREDS_FILE", cred),
            ):
                self.assertIsNone(creds.kc_read_active())  # 없으면 None (키체인 계약)
                creds.kc_write_active('{"claudeAiOauth": {"accessToken": "t"}}')
                self.assertEqual(creds.kc_read_active(),
                                 '{"claudeAiOauth": {"accessToken": "t"}}')

    def test_slot_backup_roundtrip_and_delete_on_windows(self):
        with tempfile.TemporaryDirectory() as root:
            st = state_mod.default_state()
            st["slots"] = {"2": {"email": "b@example.com", "enabled": True}}
            st["activeSlot"] = None
            with (
                mock.patch.object(paths, "IS_WIN", True),
                mock.patch.object(paths, "STATE_DIR", root),
                mock.patch.object(accounts, "reconcile_active_slot"),
                mock.patch.object(state_mod, "save_state"),
                mock.patch.object(logutil, "log"),
            ):
                creds.kc_write_slot(2, "blob-2")  # read-back 검증 포함
                self.assertEqual(creds.kc_read_slot(2), "blob-2")
                self.assertTrue(os.path.exists(filestore._slot_file(2)))
                accounts.delete_slot(st, 2)
                self.assertFalse(os.path.exists(filestore._slot_file(2)))
                self.assertNotIn("2", st["slots"])

    def test_login_session_is_creatable_on_windows(self):
        # 브라우저 로그인 추가는 이제 Windows도 지원(ConPTY) — 생성자가 막지 않는다.
        # close()가 Keychain 없이 임시 profile을 지우는 것까지 확인한다.
        with tempfile.TemporaryDirectory() as root:
            with (
                mock.patch.object(paths, "IS_WIN", True),
                mock.patch.object(paths, "STATE_DIR", root),
                mock.patch.object(keychain, "kc_delete",
                                  side_effect=AssertionError("Windows에서 Keychain 접근 금지")),
            ):
                session = session_mod.ClaudeLoginSession()
                profile = session.config_dir
                self.assertTrue(os.path.isdir(profile))
                session.close()
                self.assertFalse(os.path.exists(profile))

    def test_win_cmdline_wraps_non_exe_with_comspec_slash_s(self):
        # CreateProcessW는 .cmd/.bat을 직접 실행하지 못한다 — COMSPEC 래핑 확인.
        # /s + 전체 외곽 따옴표: 공백+괄호가 함께 있는 경로(Program Files (x86))가
        # cmd의 따옴표 제거 규칙에 걸려 깨지지 않는 형식이다.
        shim = r"C:\Program Files (x86)\nodejs\claude.CMD"
        with mock.patch.dict(os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}):
            cmdline = conpty_win._win_cmdline([shim, "auth", "login", "--claudeai"])
        self.assertEqual(
            cmdline,
            '"C:\\Windows\\System32\\cmd.exe" /d /s /c '
            '""C:\\Program Files (x86)\\nodejs\\claude.CMD" auth login --claudeai"')
        direct = conpty_win._win_cmdline([r"C:\bin\claude.exe", "auth", "login", "--claudeai"])
        self.assertEqual(direct, r"C:\bin\claude.exe auth login --claudeai")

    def test_win_env_block_is_sorted_and_nul_terminated(self):
        # None 값 제외, 이름 대문자 기준 정렬(A < b(B) < Path(P)), 항목별 NUL 종단.
        # create_unicode_buffer가 NUL을 하나 더 붙여 이중 NUL 종단이 완성된다.
        block = conpty_win._win_env_block({"b": "2", "A": "1", "skip": None, "Path": "x;y"})
        self.assertEqual(block, "A=1\0b=2\0Path=x;y\0")
        self.assertNotIn("skip", block)

    class _FakeConPty:
        """스크립트된 ConPTY 대역 — Windows 세션 분기(start/_drain/feed/close)를
        macOS에서 구동하기 위한 것. mock.patch(create=True)로 _WinConPty에 주입."""

        def __init__(self, argv, env, cols=0, rows=0):
            self.argv, self.env = argv, env
            self.chunks = [b"Open https://claude.ai/oauth/authorize?code=x in your browser\r\n"]
            self.written = b""
            self.exit_code = None
            self.closed = False
            self.pid = 4242

        def read_available(self, limit=4096):
            return self.chunks.pop(0) if self.chunks else b""

        def write(self, data):
            if self.closed:
                raise OSError("closed")
            self.written += data

        def poll(self):
            return self.exit_code

        def close(self):
            self.closed = True
            if self.exit_code is None:
                self.exit_code = 1

    def test_windows_session_start_feed_and_close_via_fake_backend(self):
        with tempfile.TemporaryDirectory() as root:
            with (
                mock.patch.object(paths, "IS_WIN", True),
                mock.patch.object(paths, "STATE_DIR", root),
                mock.patch.object(keychain, "kc_delete",
                                  side_effect=AssertionError("Windows에서 Keychain 접근 금지")),
                mock.patch.object(session_mod, "find_claude_bin", return_value=r"C:\bin\claude.exe"),
                mock.patch.object(conpty_win, "_WinConPty", self._FakeConPty, create=True),
            ):
                session = session_mod.ClaudeLoginSession()
                info = session.start(timeout=3.0)
                self.assertIn("oauth/authorize", info["url"] or "")
                fake = session.win
                self.assertEqual(fake.argv[0], r"C:\bin\claude.exe")
                self.assertEqual(fake.env.get("CLAUDE_CONFIG_DIR"), session.config_dir)
                session.feed("code-123")
                self.assertTrue(fake.written.endswith(b"code-123\r"))  # ConPTY Enter는 \r
                session.close()
                self.assertTrue(fake.closed)
                self.assertFalse(os.path.exists(session.config_dir))
                with self.assertRaisesRegex(errors.LimitPilotError, "다시 시작"):
                    session.feed("late")  # 종료 후 주입은 명확한 오류

    def test_windows_wait_for_credentials_reads_exit_code_before_close(self):
        with tempfile.TemporaryDirectory() as root:
            with (
                mock.patch.object(paths, "IS_WIN", True),
                mock.patch.object(paths, "STATE_DIR", root),
                mock.patch.object(keychain, "kc_delete",
                                  side_effect=AssertionError("Windows에서 Keychain 접근 금지")),
                mock.patch.object(session_mod, "find_claude_bin", return_value=r"C:\bin\claude.exe"),
                mock.patch.object(conpty_win, "_WinConPty", self._FakeConPty, create=True),
                mock.patch.object(session_mod, "load_isolated_login",
                                  return_value=("blob", {"emailAddress": "w@e.com"})),
            ):
                session = session_mod.ClaudeLoginSession()
                session.start(timeout=3.0)
                session.win.exit_code = 0  # 로그인 정상 완료를 시뮬레이션
                blob, identity = session.wait_for_credentials(timeout=5.0)
                self.assertEqual(blob, "blob")
                self.assertEqual(identity["emailAddress"], "w@e.com")
                self.assertTrue(session.win is None)  # finally에서 close 완료

    def test_login_credentials_come_from_profile_file_on_windows(self):
        # Windows 수확 경로: Keychain을 건드리지 않고 profile의 credentials 파일을 읽는다
        oauth = {"claudeAiOauth": {
            "accessToken": "at-1", "refreshToken": "rt-1",
            "expiresAt": 4102444800000,
            "scopes": sorted(constants.LOGIN_REQUIRED_SCOPES),
        }}
        account = {"emailAddress": "win@example.com",
                   "accountUuid": "acc-uuid", "organizationUuid": "org-uuid"}
        with tempfile.TemporaryDirectory() as profile:
            with open(os.path.join(profile, ".credentials.json"), "w", encoding="utf-8") as f:
                json.dump(oauth, f)
            with open(os.path.join(profile, ".claude.json"), "w", encoding="utf-8") as f:
                json.dump({"oauthAccount": account}, f)
            with (
                mock.patch.object(paths, "IS_WIN", True),
                mock.patch.object(keychain, "kc_read",
                                  side_effect=AssertionError("Windows에서 Keychain 접근 금지")),
            ):
                blob, identity = session_mod.load_isolated_login(profile)
        self.assertEqual(json.loads(blob), oauth)
        self.assertEqual(identity["emailAddress"], "win@example.com")

    def test_install_daemon_registers_schtasks_on_windows(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as root:
            with (
                mock.patch.object(paths, "IS_WIN", True),
                mock.patch.object(paths, "STATE_DIR", root),
                mock.patch.object(deploy, "deploy_daemon_script",
                                  return_value=r"C:\daemon\limitpilot.pyz"),
                mock.patch.object(deploy, "find_python", return_value=r"C:\py\pythonw.exe"),
                mock.patch.object(subprocess, "run", return_value=completed) as run,
                mock.patch.object(logutil, "log"),
            ):
                self.assertEqual(cli.cmd_install_daemon(None), 0)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["schtasks", "/Create"])
        self.assertIn(paths.WIN_TASK_NAME, argv)
        self.assertIn("MINUTE", argv)
        self.assertIn(r'"C:\py\pythonw.exe" "C:\daemon\limitpilot.pyz" --daemon auto --once',
                      argv)

    def test_uninstall_daemon_deletes_schtasks_on_windows(self):
        # STATE_DIR 샌드박스 필수 — uninstall은 배포 사본 파일을 실제로 지우므로
        # 이를 빼먹으면 이 머신에서 돌고 있는 진짜 데몬 사본이 삭제된다
        with tempfile.TemporaryDirectory() as root:
            task_row = f'"\\{paths.WIN_TASK_NAME}","N/A","Ready"\n'
            with (
                mock.patch.object(paths, "IS_WIN", True),
                mock.patch.object(paths, "STATE_DIR", root),
                mock.patch.object(subprocess, "run", side_effect=[
                    mock.Mock(returncode=0, stdout=task_row),
                    mock.Mock(returncode=1),  # /End: task is not currently running
                    mock.Mock(returncode=0),  # /Delete
                    mock.Mock(returncode=0, stdout=""),
                ]) as run,
                mock.patch.object(locks, "state_lock", return_value=nullcontext()),
                mock.patch.object(logutil, "log"),
            ):
                self.assertEqual(cli.cmd_uninstall_daemon(None), 0)
        self.assertEqual(run.call_args_list[2].args[0][:3],
                         ["schtasks", "/Delete", "/F"])
        self.assertEqual(run.call_args_list[-1].args[0][:2],
                         ["schtasks", "/Query"])

    def test_notify_uses_powershell_toast_on_windows(self):
        with (
            mock.patch.object(paths, "IS_WIN", True),
            mock.patch.object(subprocess, "run") as run,
        ):
            notify.notify("제목", "본문")
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "powershell")
        env = run.call_args.kwargs["env"]
        # 인젝션 차단: 제목/본문은 스크립트 문자열이 아니라 환경변수로 전달
        self.assertEqual(env["LP_TITLE"], "제목")
        self.assertEqual(env["LP_BODY"], "본문")

    def test_platform_gate_allows_windows(self):
        with (
            mock.patch.object(sys, "argv", ["limitpilot", "list"]),
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.object(deploy, "maybe_refresh_daemon_copy"),
            mock.patch.object(cli, "cmd_list", return_value=0) as cl,
        ):
            self.assertEqual(cli.main(), 0)
        cl.assert_called_once()


class DryRunPreviewTests(unittest.TestCase):
    def test_dry_run_carry_accumulates_unhealthy_ticks_across_ticks(self):
        def fresh_state():
            st = state_mod.default_state()
            st["activeSlot"] = 1
            st["settings"] = {"threshold": 90}
            st["slots"] = {"1": {"enabled": True}, "2": {"enabled": True}}
            return st

        carry = {}
        # 실제 ~/.limitpilot의 .daemon-stopped/.purging에 영향받지 않게 격리
        state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(state_dir.cleanup)
        with (
            mock.patch.object(paths, "STATE_DIR", state_dir.name),
            mock.patch.object(locks, "state_lock", return_value=nullcontext()),
            mock.patch.object(state_mod, "load_state", side_effect=lambda: fresh_state()),
            mock.patch.object(accounts, "reconcile_active_slot"),
            mock.patch.object(collector, "collect_usage", return_value={1: None, 2: None}),
            mock.patch.object(logutil, "log"),
        ):
            tick.run_tick(dry_run=True, carry=carry)
            self.assertEqual(carry["unhealthyTicks"], 1)
            tick.run_tick(dry_run=True, carry=carry)
            self.assertEqual(carry["unhealthyTicks"], 2)  # 저장 없이도 누적


class EnterpriseAccountTests(unittest.TestCase):
    """엔터프라이즈(월 달러 한도) 계정 — 세션/주간 % 대신 spend를 쓰고, 자동전환
    후보/failover에서 제외되며 수동 전환으로만 활성화된다."""

    def test_money_dollars_edges(self):
        self.assertEqual(usage_api._money_dollars({"amount_minor": 50000, "exponent": 2}), 500.0)
        self.assertEqual(usage_api._money_dollars({"amount_minor": 2, "exponent": 2}), 0.02)
        self.assertEqual(usage_api._money_dollars({"amount_minor": 0, "exponent": 2}), 0.0)
        self.assertEqual(usage_api._money_dollars({"amount_minor": 500, "exponent": 0}), 500.0)
        self.assertIsNone(usage_api._money_dollars({"amount_minor": 5}))       # exponent 없음
        self.assertIsNone(usage_api._money_dollars({"exponent": 2}))            # amount 없음
        self.assertIsNone(usage_api._money_dollars(None))
        self.assertIsNone(usage_api._money_dollars({"amount_minor": True, "exponent": 2}))  # bool 거부
        self.assertIsNone(usage_api._money_dollars({"amount_minor": 5, "exponent": -1}))    # 음수 거부

    def test_normalize_extracts_spend_and_survives_without_windows(self):
        u = usage_api.normalize_usage({
            "five_hour": None, "seven_day": None, "limits": [],
            "spend": {"used": {"amount_minor": 4250, "exponent": 2, "currency": "USD"},
                      "limit": {"amount_minor": 50000, "exponent": 2, "currency": "USD"},
                      "percent": 8, "enabled": True}})
        self.assertIsNotNone(u)  # 세션/주간이 없어도 spend가 있으면 unknown 아님
        self.assertEqual(u["spend"],
                         {"used": 42.5, "limit": 500.0, "currency": "USD", "pct": 8.5})
        self.assertEqual(u["windows"], [])
        self.assertIsNone(limits.binding_pct(u))       # 결정용 사용률에는 안 섞임
        self.assertEqual(limits.spend_pct(u), 8.5)
        self.assertEqual(limits.gate_pct(u), 8.5)      # 케이던스는 달러 소진율로

    def test_subscription_spend_disabled_returns_none(self):
        # 구독은 spend.enabled=false·limit=null → spend 없음(세션/주간만 표시)
        sub = usage_api.normalize_usage({
            "limits": [{"kind": "session", "percent": 11, "resets_at": "2099-01-01T00:00:00Z"}],
            "spend": {"enabled": False, "limit": None,
                      "used": {"amount_minor": 0, "exponent": 2, "currency": "USD"}}})
        self.assertIsNone(sub["spend"])
        self.assertEqual(limits.binding_pct(sub), 11.0)
        self.assertIsNone(usage_api.spend_from_usage_raw({"spend": {"enabled": True, "limit": None,
            "used": {"amount_minor": 0, "exponent": 2}}}))  # limit 없으면 None

    def test_pct_computed_from_dollars_not_api_integer(self):
        # API가 정수로 반올림한 percent 대신 used/limit로 계산해 달러 표시와 일치
        u = usage_api.normalize_usage({"limits": [], "spend": {"enabled": True, "percent": 0,
            "used": {"amount_minor": 300, "exponent": 2, "currency": "USD"},
            "limit": {"amount_minor": 50000, "exponent": 2, "currency": "USD"}}})
        self.assertEqual(u["spend"]["pct"], 0.6)  # $3/$500 = 0.6%(API는 0)

    def test_is_enterprise_slot(self):
        self.assertTrue(limits.is_enterprise_slot({"oauthAccount": ENTERPRISE_OAUTH}))
        self.assertFalse(limits.is_enterprise_slot({"oauthAccount": {"organizationType": "claude_team"}}))
        self.assertFalse(limits.is_enterprise_slot({"oauthAccount": {}}))
        self.assertFalse(limits.is_enterprise_slot({}))
        self.assertFalse(limits.is_enterprise_slot(None))

    def _active_ent_state(self):
        st = state_mod.default_state()
        st["activeSlot"] = 5
        st["settings"] = {"threshold": 90}
        st["unhealthyTicks"] = constants.UNHEALTHY_TICKS - 1  # 한 틱 뒤면 failover될 직전
        st["slots"] = {
            "5": {"enabled": True, "oauthAccount": ENTERPRISE_OAUTH},
            "1": {"enabled": True, "oauthAccount": {"organizationType": "claude_team",
                                                    "accountUuid": "a", "organizationUuid": "b"}},
        }
        return st

    def test_enterprise_active_holds_never_failover(self):
        st = self._active_ent_state()
        action, target, reason = decide.decide(st, {5: _ent_usage(), 1: _usage(10)})
        self.assertEqual(action, "none")
        self.assertIsNone(target)
        self.assertEqual(st["unhealthyTicks"], 0)   # 카운터 리셋(거짓 failover 방지)
        self.assertIn("엔터프라이즈", reason)

    def test_enterprise_active_holds_even_when_unmeasurable(self):
        # 활성 usage 조회 실패(None)여도 신원 기반으로 관망 — 구독 오전환 금지(요구사항)
        st = self._active_ent_state()
        action, target, _ = decide.decide(st, {5: None, 1: _usage(10)})
        self.assertEqual(action, "none")
        self.assertEqual(st["unhealthyTicks"], 0)

    def test_enterprise_excluded_from_auto_candidates(self):
        st = state_mod.default_state()
        st["activeSlot"] = 1
        st["settings"] = {"threshold": 90}
        st["slots"] = {
            "1": {"enabled": True, "oauthAccount": {"organizationType": "claude_team"}},
            "5": {"enabled": True, "oauthAccount": ENTERPRISE_OAUTH},
        }
        # 활성 소진(100%)이고 유일 후보가 엔터프라이즈 → 전환 대상 없음(blocked)
        action, target, _ = decide.decide(st, {1: _usage(100), 5: _ent_usage()})
        self.assertNotEqual(target, 5)
        self.assertEqual(action, "blocked")
        self.assertIsNone(decide.best_candidate(st, 1))  # best_candidate도 제외

    def test_enterprise_manual_switch_allowed_when_off(self):
        # OFF 엔터프라이즈도 수동 활성 허용(enabled 무관) — dashboard.mutate_slot 경로가
        # OFF 오류를 던지지 않고 perform_switch로 위임해야 한다
        state = state_mod.default_state()
        state["slots"]["5"] = {
            "email": "e@corp.com", "alias": "Corp", "enabled": False, "kind": "oauth",
            "oauthAccount": ENTERPRISE_OAUTH}
        with (
            mock.patch.object(locks, "state_lock", return_value=nullcontext()),
            mock.patch.object(state_mod, "load_state", return_value=state),
            mock.patch.object(switch_mod, "perform_switch", return_value="switched") as switch,
        ):
            msg = server.mutate_slot(5, "activate", force=False)
        switch.assert_called_once_with(state, 5, force=False)
        self.assertIn("Corp", msg)


class DashboardEnterpriseTests(unittest.TestCase):
    def test_slot_status_exposes_spend_for_enterprise_snapshot(self):
        st = state_mod.default_state()
        st["activeSlot"] = None
        st["slots"] = {"5": {"email": "e@corp.com", "enabled": False, "kind": "oauth",
                             "oauthAccount": ENTERPRISE_OAUTH,
                             "usage": _ent_usage(42.5, 500.0), "usageFetchedAt": 123.0}}
        out = status.slot_status(st, 5, active=False, lang="ko")
        self.assertTrue(out["enterprise"])
        self.assertEqual(out["spend"],
                         {"used": 42.5, "limit": 500.0, "currency": "USD", "pct": 8.5})
        self.assertEqual(out["windows"], [])
        self.assertEqual(out["fetchedAt"], 123.0)


class GatewayRoundTripTests(unittest.TestCase):
    def _state(self):
        st = state_mod.default_state()
        st["activeSlot"] = 1
        st["slots"] = {
            "1": {"email": "sub@example.com", "enabled": True, "kind": "oauth",
                  "oauthAccount": {"emailAddress": "sub@example.com",
                                   "accountUuid": "acct", "organizationUuid": "org"}},
            "2": {"alias": "Example Gateway", "enabled": True, "kind": "gateway",
                  "baseUrl": "https://gateway.example.invalid",
                  "modelId": "example-model", "authMode": "bearer"},
        }
        return st

    def test_oauth_gateway_same_oauth_round_trip_restores_settings(self):
        st = self._state()
        live_blob = '{"claudeAiOauth":{"accessToken":"oauth-access"}}'
        with tempfile.TemporaryDirectory() as tmp:
            settings_file = os.path.join(tmp, ".claude", "settings.json")
            os.makedirs(os.path.dirname(settings_file))
            original = {"env": {"KEEP_ME": "yes", "ANTHROPIC_MODEL": "original"},
                        "permissions": {"allow": ["Read"]}}
            filestore.atomic_write_json(settings_file, original)
            with (
                mock.patch.object(paths, "CLAUDE_SETTINGS_FILE", settings_file),
                mock.patch.object(state_mod, "save_state"),
                mock.patch.object(deploy, "find_python", return_value="/usr/bin/python3"),
                mock.patch.object(creds, "kc_read_slot",
                                  side_effect=lambda n: "gateway-secret" if n == 2 else live_blob),
                mock.patch.object(creds, "kc_read_active", return_value=live_blob),
                mock.patch.object(creds, "kc_write_slot") as backup,
                mock.patch.object(accounts, "read_live_oauth_account",
                                  return_value=st["slots"]["1"]["oauthAccount"]),
                mock.patch.object(locks, "proper_lockfile", return_value=nullcontext()),
            ):
                self.assertEqual(switch_mod.perform_switch(st, 2), "switched")
                active_settings = filestore.read_json(settings_file)
                self.assertEqual(active_settings["env"]["ANTHROPIC_MODEL"], "example-model")
                self.assertNotIn("ANTHROPIC_AUTH_TOKEN", active_settings["env"])
                self.assertIn("gateway-key 2", active_settings["apiKeyHelper"])

                self.assertEqual(switch_mod.perform_switch(st, 1), "already")
                self.assertEqual(filestore.read_json(settings_file), original)
                self.assertEqual(st["activeSlot"], 1)
                self.assertNotIn("gatewayRestore", st)
            backup.assert_called_once_with(1, live_blob)

    def test_failed_oauth_switch_keeps_gateway_routing_active(self):
        st = self._state()
        st["activeSlot"] = 2
        live_identity = {"emailAddress": "other@example.com",
                         "accountUuid": "other", "organizationUuid": "other-org"}
        with tempfile.TemporaryDirectory() as tmp:
            settings_file = os.path.join(tmp, ".claude", "settings.json")
            with (
                mock.patch.object(paths, "CLAUDE_SETTINGS_FILE", settings_file),
                mock.patch.object(state_mod, "save_state"),
                mock.patch.object(deploy, "find_python", return_value="/usr/bin/python3"),
                mock.patch.object(creds, "kc_read_slot", return_value="gateway-secret"),
            ):
                gateway.activate_gateway_settings(st, 2)
                with (
                    mock.patch.object(creds, "kc_read_active", return_value="live-oauth"),
                    mock.patch.object(accounts, "read_live_oauth_account", return_value=live_identity),
                    mock.patch.object(locks, "proper_lockfile", return_value=nullcontext()),
                ):
                    with self.assertRaises(errors.SwitchError):
                        switch_mod.perform_switch(st, 1)
                self.assertTrue(gateway.gateway_config_is_active(st, 2))
                self.assertIn("gatewayRestore", st)

    def test_reconcile_prefers_active_gateway_settings_over_underlying_oauth(self):
        st = self._state()
        # Simulate the stale state produced by an older dashboard poll: OAuth slot
        # 1 in state while settings still route every request through gateway 2.
        st["activeSlot"] = 1
        st["gatewayRestore"] = {"settingsFilePresent": True}
        with (
            mock.patch.object(gateway, "gateway_config_is_active",
                              side_effect=lambda state, n: n == 2),
            mock.patch.object(accounts, "read_live_oauth_account") as read_oauth,
        ):
            binding = accounts.reconcile_active_slot(st)
        self.assertEqual(binding["status"], "managed")
        self.assertEqual(binding["slot"], 2)
        self.assertTrue(binding["changed"])
        self.assertEqual(st["activeSlot"], 2)
        read_oauth.assert_not_called()

    def test_reconcile_preserves_state_during_incomplete_gateway_restore(self):
        st = self._state()
        st["gatewayRestore"] = {"settingsFilePresent": True}
        with (
            mock.patch.object(gateway, "gateway_config_is_active", return_value=False),
            mock.patch.object(accounts, "read_live_oauth_account") as read_oauth,
        ):
            binding = accounts.reconcile_active_slot(st)
        self.assertEqual(binding["status"], "unknown")
        self.assertEqual(st["activeSlot"], 1)
        read_oauth.assert_not_called()

    def test_gateway_profile_rejects_unsafe_url_and_multiline_secret(self):
        with self.assertRaises(errors.LimitPilotError):
            gateway.validate_gateway_profile("http://gateway.example", "model", "token")
        with self.assertRaises(errors.LimitPilotError):
            gateway.validate_gateway_profile("https://user:pass@gateway.example", "model", "token")
        with self.assertRaises(errors.LimitPilotError):
            gateway.validate_gateway_profile("https://gateway.example", "model", "line1\nline2")

    def test_dashboard_gateway_add_tests_before_store_and_never_returns_token(self):
        body = {"baseUrl": "https://gateway.example.invalid", "modelId": "example-model",
                "token": "super-secret", "authMode": "bearer", "alias": "Example Gateway",
                "inputTokenLimit": "1000000", "outputTokenLimit": "200000",
                "inputPricePerMillion": "2.5", "outputPricePerMillion": "10",
                "costLimit": "25"}
        state = state_mod.default_state()
        calls = []
        with (
            mock.patch.object(gateway, "probe_gateway_connection",
                              side_effect=lambda *args: calls.append("probe")) as probe,
            mock.patch.object(locks, "state_lock", return_value=nullcontext()),
            mock.patch.object(state_mod, "load_state", return_value=state),
            mock.patch.object(gateway, "register_gateway_slot",
                              side_effect=lambda *args: (calls.append("store") or (4, "Example Gateway", True))) as store,
        ):
            result = server.add_gateway(body)
        self.assertEqual(calls, ["probe", "store"])
        probe.assert_called_once_with("https://gateway.example.invalid", "example-model",
                                      "super-secret", "bearer")
        self.assertNotIn("super-secret", str(result))
        config = store.call_args.args[-1]
        self.assertEqual(config, {"inputTokenLimit": 1000000, "outputTokenLimit": 200000,
                                  "inputPricePerMillion": 2.5,
                                  "outputPricePerMillion": 10.0, "costLimit": 25.0})
        self.assertEqual(result["slot"], 4)
        self.assertIn("등록", result["message"])

    def test_dashboard_gateway_edit_keeps_saved_token_when_blank(self):
        body = {"baseUrl": "https://changed.example", "modelId": "changed-model",
                "token": "", "authMode": "api-key", "alias": "Changed",
                "inputTokenLimit": 0, "outputTokenLimit": 5000,
                "inputPricePerMillion": 1.25, "outputPricePerMillion": 5,
                "costLimit": 0}
        state = state_mod.default_state()
        state["slots"]["4"] = {"kind": "gateway", "baseUrl": "https://old.example",
                                  "modelId": "old-model", "authMode": "bearer"}
        calls = []
        with (
            mock.patch.object(locks, "state_lock", return_value=nullcontext()),
            mock.patch.object(state_mod, "load_state", return_value=state),
            mock.patch.object(creds, "kc_read_slot", return_value="saved-secret") as read_secret,
            mock.patch.object(gateway, "probe_gateway_connection",
                              side_effect=lambda *args: calls.append(("probe", args))),
            mock.patch.object(gateway, "update_gateway_slot",
                              side_effect=lambda *args: (calls.append(("update", args)) or (4, "Changed"))),
        ):
            result = server.edit_gateway(4, body)
        read_secret.assert_called_once_with(4)
        self.assertEqual(calls[0], ("probe", ("https://changed.example", "changed-model",
                                               "saved-secret", "api-key")))
        self.assertEqual(calls[1][0], "update")
        self.assertEqual(calls[1][1][4], "saved-secret")
        self.assertEqual(calls[1][1][-1]["outputTokenLimit"], 5000)
        self.assertNotIn("saved-secret", str(result))
        self.assertIn("수정", result["message"])

    def test_gateway_form_examples_are_provider_neutral(self):
        if True:
            html = server.read_html().decode("utf-8")
        self.assertIn("gateway.example.invalid", html)
        self.assertIn("example-model-id", html)
        self.assertIn('value="${esc(gatewayDraft.baseUrl)}"', html)
        self.assertIn('value="${esc(gatewayDraft.modelId)}"', html)
        self.assertIn('id="gwInputLimit"', html)
        self.assertIn('id="gwOutputLimit"', html)
        self.assertIn('id="gwInputPrice"', html)
        self.assertIn('id="gwOutputPrice"', html)
        self.assertIn('id="gwCostLimit"', html)
        self.assertIn('function gatewayModeToggle(s)', html)

    def test_gateway_usage_aggregates_since_registration_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp, ".claude", "projects", "sample")
            project.mkdir(parents=True)
            records = [
                {"type": "assistant", "message": {"id": "msg-1", "model": "example-model",
                 "usage": {"input_tokens": 10, "output_tokens": 2,
                           "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4}}},
                # Same streamed/transcript message: keep component maxima, don't add twice.
                {"type": "assistant", "message": {"id": "msg-1", "model": "example-model",
                 "usage": {"input_tokens": 12, "output_tokens": 2,
                           "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4}}},
                {"type": "assistant", "message": {"id": "msg-other", "model": "other-model",
                 "usage": {"input_tokens": 999, "output_tokens": 999}}},
            ]
            with open(project / "session.jsonl", "w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")
            slot = {"modelId": "example-model", "addedAt": "2000-01-01T00:00:00+00:00"}
            with mock.patch.object(paths, "HOME", tmp):
                with gateway_usage._gateway_usage_lock:
                    gateway_usage._gateway_usage_cache.clear()
                usage = gateway_usage.gateway_token_usage(slot)
            self.assertEqual(usage, {"input": 12, "output": 2, "cacheRead": 3,
                                     "cacheCreation": 4, "total": 21, "responses": 1})

    def test_gateway_usage_meter_supports_unlimited_tokens_and_combined_cost(self):
        slot = {"gatewayUsageConfig": {
            "inputTokenLimit": 0, "outputTokenLimit": 100,
            "inputPricePerMillion": 2, "outputPricePerMillion": 10,
            "costLimit": 1,
        }}
        meter = status.gateway_usage_meter(slot, {
            "input": 100, "cacheRead": 50, "cacheCreation": 25, "output": 20,
        })
        self.assertEqual(meter["input"], {"used": 175, "limit": 0, "pct": None})
        self.assertEqual(meter["output"], {"used": 20, "limit": 100, "pct": 20.0})
        self.assertTrue(meter["cost"]["enabled"])
        self.assertAlmostEqual(meter["cost"]["input"], 0.00035)
        self.assertAlmostEqual(meter["cost"]["output"], 0.0002)
        self.assertAlmostEqual(meter["cost"]["used"], 0.00055)
        self.assertAlmostEqual(meter["cost"]["pct"], 0.055)

    def test_gateway_usage_config_rejects_negative_and_fractional_token_limits(self):
        with self.assertRaises(errors.LimitPilotError):
            gateway.validate_gateway_usage_config(-1, 0, 0, 0, 0)
        with self.assertRaises(errors.LimitPilotError):
            gateway.validate_gateway_usage_config(1.5, 0, 0, 0, 0)


class DashboardReuseWindowTests(unittest.TestCase):
    """대시보드를 다시 열 때 이미 창이 있으면 새 창을 만들지 않고 앞으로 가져온다."""

    def _args(self, no_open=False, browser="auto"):
        return types.SimpleNamespace(no_open=no_open, browser=browser)

    def test_open_window_is_raised_not_duplicated(self):
        with (mock.patch.object(server, "focus_ui", return_value=True) as focus,
              mock.patch.object(server, "open_ui") as open_ui):
            server._reuse_running("http://x", self._args(), {"clients": 1})
        focus.assert_called_once()
        open_ui.assert_not_called()

    def test_open_window_not_duplicated_even_if_focus_fails(self):
        # 프레즌스가 창을 알고 있으면 focus 실패해도 중복 창을 열지 않는다
        with (mock.patch.object(server, "focus_ui", return_value=False),
              mock.patch.object(server, "open_ui") as open_ui):
            server._reuse_running("http://x", self._args(), {"clients": 2})
        open_ui.assert_not_called()

    def test_no_window_opens_new_when_focus_finds_nothing(self):
        with (mock.patch.object(server, "focus_ui", return_value=False),
              mock.patch.object(server, "open_ui") as open_ui):
            server._reuse_running("http://x", self._args(), {"clients": 0})
        open_ui.assert_called_once()

    def test_no_window_raises_existing_when_focus_finds_one(self):
        with (mock.patch.object(server, "focus_ui", return_value=True),
              mock.patch.object(server, "open_ui") as open_ui):
            server._reuse_running("http://x", self._args(), {"clients": 0})
        open_ui.assert_not_called()

    def test_no_open_flag_never_opens_or_focuses(self):
        with (mock.patch.object(server, "focus_ui") as focus,
              mock.patch.object(server, "open_ui") as open_ui):
            server._reuse_running("http://x", self._args(no_open=True), {"clients": 0})
        focus.assert_not_called()
        open_ui.assert_not_called()


if __name__ == "__main__":
    unittest.main()
