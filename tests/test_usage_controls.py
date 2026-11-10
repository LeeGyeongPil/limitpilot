"""수동 새로고침 · 일일 유지 호출 · 주간 리셋 임박 우선 · 계정별 주간 상한."""

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from unittest import mock

from limitpilot import accounts
from limitpilot import cli
from limitpilot import collector
from limitpilot import constants
from limitpilot import errors
from limitpilot import locks
from limitpilot import logutil
from limitpilot import notify as notify_mod
from limitpilot import oauth
from limitpilot import state
from limitpilot import tick
from limitpilot import usage_api
from limitpilot.daemon import deploy as deploy_mod
from limitpilot.dashboard import runtime
from limitpilot.dashboard import server
from limitpilot.dashboard import status as status_mod
from limitpilot.policy import decide
from limitpilot.policy import limits
from limitpilot.store import creds


def _iso(offset_s):
    return datetime.fromtimestamp(time.time() + offset_s, timezone.utc).isoformat()


def _u(session=0.0, weekly=0.0, weekly_reset_s=6 * 86400, session_reset_s=3600):
    """정규화 usage — 세션/주간 사용률과 리셋까지 남은 초."""
    return usage_api.normalize_usage({"limits": [
        {"kind": "session", "percent": session,
         "resets_at": _iso(session_reset_s) if session_reset_s is not None else None},
        {"kind": "weekly_all", "percent": weekly,
         "resets_at": _iso(weekly_reset_s) if weekly_reset_s is not None else None},
    ]})


def _state(n=3, threshold=90, priority=None):
    st = state.default_state()
    st["activeSlot"] = 1
    # 이식한 테스트는 기능이 켜진 상태를 검증한다(공개 기본값은 test_defaults_and_ux 에서)
    st["settings"] = {"threshold": threshold, "finalBurn": True, "keepalive": True,
                      "autoSwitch": True}
    if priority:
        st["settings"]["priority"] = priority
    st["slots"] = {str(i): {"enabled": True, "email": f"s{i}@example.com"}
                   for i in range(1, n + 1)}
    return st


class QuietTestCase(unittest.TestCase):
    """실제 state.json·limitpilot.log를 건드리지 않게 저장·로그를 막는다."""

    def setUp(self):
        for module, name in ((state, "save_state"), (logutil, "log"), (notify_mod, "notify")):
            patcher = mock.patch.object(module, name)
            patcher.start()
            self.addCleanup(patcher.stop)


class WeeklyCapTests(QuietTestCase):
    def test_cap_defaults_and_clamps(self):
        self.assertEqual(limits.weekly_cap({}), 100.0)
        self.assertEqual(limits.weekly_cap({"weeklyCap": 80}), 80.0)
        self.assertEqual(limits.weekly_cap({"weeklyCap": 5}), constants.WEEKLY_CAP_MIN)
        self.assertEqual(limits.weekly_cap({"weeklyCap": "junk"}), 100.0)
        self.assertEqual(limits.weekly_cap({"weeklyCap": float("nan")}), 100.0)

    def test_normalize_maps_threshold_to_base_and_limit_to_100(self):
        n = limits.normalize_pct
        self.assertEqual(n(50, 90, 100, 90), 50)          # 전역과 같으면 항등
        self.assertEqual(n(95, 90, 100, 90), 95)
        self.assertAlmostEqual(n(80, 80, 100, 95), 95)    # 계정 임계 → 전역 세션 임계
        self.assertAlmostEqual(n(90, 80, 100, 95), 97.5)  # 임계~최대 사이 선형
        self.assertEqual(n(100, 80, 100, 95), 100)
        self.assertEqual(n(80, 90, 80, 90), 100)          # 임계가 최대치에 잘림 — 즉시
        self.assertLess(n(79, 90, 80, 90), 90)            # 최대치 직전은 임계 미만
        u = _u(session=30, weekly=60)
        eff = limits.capped_usage(u, 80)
        self.assertEqual(eff["five_hour"]["pct"], 30.0)
        self.assertEqual(u["seven_day"]["pct"], 60.0)  # 원본 불변

    def test_fractional_threshold_is_exact_without_overrides(self):
        for g in (51.6, 77.7, 93.3):
            self.assertEqual(limits.normalize_pct(g, g, 100, g), g)
        st = _state(2, threshold=51.6)
        self.assertEqual(decide.decide(st, {1: _u(51.6, 10), 2: _u(1, 1)})[:2], ("switch", 2))

    def test_weekly_room_is_measured_against_cap_in_raw_points(self):
        # 리뷰 회귀: 최대치 80·주간 75%면 실제 여유 5%p — 리셋 임박 이동 대상이 아니다
        st = _state(2)
        st["slots"]["2"]["weeklyCap"] = 80
        st["lastSwitchAt"] = time.time() - 7200
        eff = limits.slot_decision_usage(st, st["slots"]["2"], _u(3, 75, weekly_reset_s=20 * 3600))
        self.assertEqual(limits.weekly_headroom(eff), 5.0)
        self.assertEqual(decide.decide(st, {1: _u(5, 10), 2: _u(3, 75, weekly_reset_s=20 * 3600)})[0], "none")

    def test_per_account_weekly_threshold_triggers_proactive(self):
        st = _state(2)
        st["slots"]["1"]["weeklyThreshold"] = 70
        action, target, _ = decide.decide(st, {1: _u(10, 70), 2: _u(10, 20)})
        self.assertEqual((action, target), ("switch", 2))
        self.assertEqual(decide.decide(st, {1: _u(10, 69), 2: _u(10, 20)})[0], "none")

    def test_per_account_session_threshold_and_global_weekly(self):
        st = _state(2)
        st["slots"]["1"]["sessionThreshold"] = 60
        self.assertEqual(decide.decide(st, {1: _u(60, 10), 2: _u(10, 20)})[:2], ("switch", 2))
        st = _state(2)
        st["settings"]["weeklyThreshold"] = 70     # 전역 주간 임계 — 세션은 그대로 90
        self.assertEqual(decide.decide(st, {1: _u(10, 70), 2: _u(10, 20)})[:2], ("switch", 2))
        self.assertEqual(decide.decide(st, {1: _u(85, 60), 2: _u(10, 20)})[0], "none")

    def test_global_weekly_threshold_defaults_to_session(self):
        st = _state(1)
        cfg = state.get_settings(st)
        self.assertEqual((cfg["weeklyThreshold"], cfg["weeklyThresholdSet"]), (90.0, False))
        cfg = state.set_setting(st, "weeklyThreshold", 75)
        self.assertEqual((cfg["weeklyThreshold"], cfg["weeklyThresholdSet"]), (75.0, True))
        self.assertNotIn("weeklyThresholdSet", st["settings"])  # 파생값은 저장 안 함
        cfg = state.set_setting(st, "weeklyThreshold", None)
        self.assertEqual(cfg["weeklyThreshold"], 90.0)
        self.assertNotIn("weeklyThreshold", st["settings"])
        with self.assertRaises(errors.LimitPilotError):
            state.set_setting(st, "weeklyThreshold", 30)

    def test_set_slot_limits_validates_and_resets(self):
        st = _state(2)
        lim = limits.set_slot_limits(st, 2, session=80, weekly="70", cap=85)
        self.assertEqual((lim["session"], lim["weekly"], lim["cap"]), (80.0, 70.0, 85.0))
        self.assertTrue(lim["sessionOverride"] and lim["weeklyOverride"])
        lim = limits.set_slot_limits(st, 2, session=None, weekly="default", cap=None)
        self.assertEqual((lim["session"], lim["weekly"], lim["cap"]), (90.0, 90.0, 100.0))
        self.assertFalse(lim["sessionOverride"] or lim["weeklyOverride"])
        for bad in ({"session": 20}, {"weekly": "x"}, {"cap": 5}, {"session": True}):
            with self.assertRaises(errors.LimitPilotError):
                limits.set_slot_limits(st, 2, **bad)
        lim = limits.set_slot_limits(st, 2, weekly=95, cap=80)
        self.assertEqual(lim["weekly"], 80.0)  # 주간 임계는 최대치를 넘지 않는다
        st["slots"]["2"]["kind"] = constants.GATEWAY_KIND
        with self.assertRaises(errors.LimitPilotError):
            limits.set_slot_limits(st, 2, session=80)

    def test_active_reaching_cap_switches_immediately(self):
        st = _state(2)
        st["slots"]["1"]["weeklyCap"] = 80
        st["lastSwitchAt"] = time.time()  # 쿨다운 중이어도 상한 도달은 즉시
        action, target, reason = decide.decide(st, {1: _u(10, 80), 2: _u(10, 20)})
        self.assertEqual((action, target), ("switch", 2))
        self.assertIn("주간 상한 80%", reason)

    def test_candidate_at_cap_is_excluded(self):
        st = _state(3)
        st["slots"]["2"]["weeklyCap"] = 50
        action, target, _ = decide.decide(st, {1: _u(100, 10), 2: _u(0, 50), 3: _u(0, 70)})
        self.assertEqual((action, target), ("switch", 3))

    def test_all_capped_candidates_block_as_exhausted(self):
        st = _state(2)
        st["slots"]["2"]["weeklyCap"] = 50
        action, _, reason = decide.decide(st, {1: _u(100, 10), 2: _u(0, 55)})
        self.assertEqual((action, reason), ("blocked", constants.reason_all_exhausted()))

    def test_best_candidate_honors_cap(self):
        st = _state(3)
        st["slots"]["2"].update(weeklyCap=40, usage=_u(0, 45))
        st["slots"]["3"]["usage"] = _u(0, 70)
        self.assertEqual(decide.best_candidate(st, 1), 3)

    def test_probe_promotion_uses_capped_active_usage(self):
        # 상한 환산으로 임계 근접이면 활성 신선 스냅샷도 매 틱 재실측된다
        st = _state(1)
        st["slots"]["1"].update(weeklyCap=50, usage=_u(5, 45),
                                usageFetchedAt=time.time() - 30,
                                oauthAccount={"emailAddress": "s1@example.com"})
        with (
            mock.patch.object(collector, "maybe_release_quarantine"),
            mock.patch.object(accounts, "live_slot_of", return_value=1),
            mock.patch.object(creds, "kc_read_active", return_value="blob"),
            mock.patch.object(oauth, "parse_oauth", return_value={"accessToken": "tok"}),
            mock.patch.object(oauth, "is_expired", return_value=False),
            mock.patch.object(usage_api, "fetch_usage", return_value=(_u(5, 46), None, None)) as fetch,
        ):
            collector.collect_usage(st)
        fetch.assert_called_once()

    def test_capped_out_slot_is_not_probed_until_weekly_reset(self):
        st = _state(2)
        st["slots"]["2"].update(weeklyCap=50, usage=_u(0, 55, weekly_reset_s=86400),
                                usageFetchedAt=time.time() - 3600)
        st["slots"]["1"].update(usage=_u(10, 10), usageFetchedAt=time.time() - 30)
        with (
            mock.patch.object(collector, "maybe_release_quarantine"),
            mock.patch.object(accounts, "live_slot_of", return_value=1),
            mock.patch.object(creds, "kc_read_active", return_value="blob"),
            mock.patch.object(creds, "kc_read_slot", return_value="blob"),
            mock.patch.object(oauth, "parse_oauth", return_value={"accessToken": "tok"}),
            mock.patch.object(oauth, "is_expired", return_value=False),
            mock.patch.object(usage_api, "fetch_usage") as fetch,
        ):
            result = collector.collect_usage(st)
        fetch.assert_not_called()
        self.assertEqual(result[2], st["slots"]["2"]["usage"])

    def test_set_weekly_cap_validates_and_clears_at_100(self):
        st = _state(2)
        self.assertEqual(limits.set_slot_weekly_cap(st, 2, "79.6"), 80.0)
        self.assertEqual(st["slots"]["2"]["weeklyCap"], 80.0)
        self.assertEqual(limits.set_slot_weekly_cap(st, 2, 100), 100.0)
        self.assertNotIn("weeklyCap", st["slots"]["2"])
        for bad in (5, 101, "x", None, True, float("nan")):
            with self.assertRaises(errors.LimitPilotError):
                limits.set_slot_weekly_cap(st, 2, bad)
        st["slots"]["2"]["kind"] = constants.GATEWAY_KIND
        with self.assertRaises(errors.LimitPilotError):
            limits.set_slot_weekly_cap(st, 2, 80)
        with self.assertRaises(errors.LimitPilotError):
            limits.set_slot_weekly_cap(st, 9, 80)


class ExpiringPriorityTests(QuietTestCase):
    def test_at_limit_prefers_weekly_reset_soonest(self):
        st = _state(3)
        usages = {1: _u(100, 30),
                  2: _u(5, 10, weekly_reset_s=6 * 86400),
                  3: _u(20, 70, weekly_reset_s=20 * 3600)}
        self.assertEqual(decide.decide(st, usages)[:2], ("switch", 3))
        st["settings"]["priority"] = "balanced"
        self.assertEqual(decide.decide(st, usages)[:2], ("switch", 2))

    def test_nearly_used_up_weekly_gets_no_priority(self):
        # 리셋이 임박해도 여유가 EXPIRING_MIN_HEADROOM_PCT 미만이면 우선권 없음
        st = _state(3)
        usages = {1: _u(100, 30),
                  2: _u(5, 10, weekly_reset_s=6 * 86400),
                  3: _u(5, 92, weekly_reset_s=30 * 3600)}
        self.assertEqual(decide.decide(st, usages)[:2], ("switch", 2))
        # 리셋 직전(5시간 안)이면 작은 여유(5%p 이상)도 먼저 쓴다 — 3%p는 여전히 제외
        usages[3] = _u(5, 95, weekly_reset_s=3600)
        self.assertEqual(decide.decide(st, usages)[:2], ("switch", 3))
        usages[3] = _u(5, 97, weekly_reset_s=3600)
        self.assertEqual(decide.decide(st, usages)[:2], ("switch", 2))

    def test_resets_within_same_bucket_fall_back_to_usage_score(self):
        st = _state(3)
        usages = {1: _u(100, 30),
                  2: _u(40, 60, weekly_reset_s=13 * 3600),
                  3: _u(5, 50, weekly_reset_s=14 * 3600)}
        self.assertEqual(decide.decide(st, usages)[:2], ("switch", 3))

    def test_unstarted_weekly_window_has_no_urgency(self):
        st = _state(3)
        usages = {1: _u(100, 30),
                  2: _u(0, 0, weekly_reset_s=None, session_reset_s=None),
                  3: _u(30, 40, weekly_reset_s=5 * 86400)}
        self.assertEqual(decide.decide(st, usages)[:2], ("switch", 3))

    def test_rebalance_moves_to_expiring_account_below_threshold(self):
        st = _state(2)
        action, target, reason = decide.decide(st, {
            1: _u(20, 18, weekly_reset_s=6 * 86400),
            2: _u(3, 81, weekly_reset_s=20 * 3600)})
        self.assertEqual((action, target), ("switch", 2))
        self.assertIn("리셋 임박 우선", reason)

    def test_rebalance_respects_cooldown_and_balanced_mode(self):
        usages = {1: _u(20, 18), 2: _u(3, 60, weekly_reset_s=10 * 3600)}
        st = _state(2)
        st["lastSwitchAt"] = time.time() - 600
        self.assertEqual(decide.decide(st, usages)[0], "none")
        st = _state(2, priority="balanced")
        self.assertEqual(decide.decide(st, usages)[0], "none")

    def test_no_rebalance_when_active_is_itself_expiring(self):
        st = _state(2)
        action, _, _ = decide.decide(st, {
            1: _u(20, 40, weekly_reset_s=30 * 3600 - 7 * 3600),
            2: _u(3, 60, weekly_reset_s=8 * 3600)})
        self.assertEqual(action, "none")

    def test_no_rebalance_to_candidate_near_threshold_weekly(self):
        st = _state(2)
        action, _, _ = decide.decide(st, {
            1: _u(20, 18), 2: _u(3, 86, weekly_reset_s=10 * 3600)})
        self.assertEqual(action, "none")

    def test_no_rebalance_to_candidate_with_busy_session(self):
        # 세션이 후보 적격선(임계−히스테리시스) 위면 옮겨도 곧 다시 전환된다
        st = _state(2)
        action, _, _ = decide.decide(st, {
            1: _u(20, 18), 2: _u(85, 60, weekly_reset_s=10 * 3600)})
        self.assertEqual(action, "none")

    def test_rebalance_skips_capped_off_and_manual_slots(self):
        expiring = _u(3, 60, weekly_reset_s=10 * 3600)
        for extra in ({"weeklyCap": 60}, {"enabled": False},
                      {"kind": constants.GATEWAY_KIND}, {"quarantine": {"reason": "x"}}):
            st = _state(2)
            st["slots"]["2"].update(extra)
            self.assertEqual(decide.decide(st, {1: _u(20, 18), 2: expiring})[0], "none", extra)

    def test_busy_session_candidate_gets_no_expiring_priority(self):
        # 리뷰 회귀: 세션 98%인 후보가 주간 리셋 임박만으로 여유 있는 후보를 이기면 안 된다
        st = _state(3)
        usages = {1: _u(100, 30),
                  2: _u(98, 40, weekly_reset_s=30 * 3600),
                  3: _u(0, 5, weekly_reset_s=6 * 86400)}
        self.assertEqual(decide.decide(st, usages)[:2], ("switch", 3))
        st["slots"]["2"]["usage"], st["slots"]["3"]["usage"] = usages[2], usages[3]
        self.assertEqual(decide.best_candidate(st, 1), 3)

    def test_rebalance_requires_session_room(self):
        # 리뷰 회귀: 세션 79%(임계 90)인 후보로 옮기면 곧 되돌아온다 — 이동 금지
        st = _state(2)
        st["lastSwitchAt"] = time.time() - 7200
        action, _, _ = decide.decide(st, {
            1: _u(5, 10), 2: _u(79, 50, weekly_reset_s=20 * 3600)})
        self.assertEqual(action, "none")

    def test_manual_choice_holds_rebalance(self):
        usages = {1: _u(20, 18), 2: _u(3, 60, weekly_reset_s=10 * 3600)}
        st = _state(2)
        st["manualChoiceAt"] = time.time() - 3600
        self.assertEqual(decide.decide(st, usages)[0], "none")
        st["manualChoiceAt"] = time.time() - constants.MANUAL_HOLD_S - 1
        self.assertEqual(decide.decide(st, usages)[0], "switch")

    def test_external_login_change_is_stamped_as_manual_choice(self):
        st = _state(2)
        with mock.patch.object(accounts, "inspect_live_account",
                               return_value={"status": "managed", "slot": 2}):
            accounts.reconcile_active_slot(st)
        self.assertEqual(st["activeSlot"], 2)
        self.assertAlmostEqual(st["manualChoiceAt"], time.time(), delta=5)
        st.pop("manualChoiceAt")
        with mock.patch.object(accounts, "inspect_live_account",
                               return_value={"status": "managed", "slot": 2}):
            accounts.reconcile_active_slot(st)  # 변화 없음 — 표시하지 않는다
        self.assertNotIn("manualChoiceAt", st)

    def test_settings_default_and_validation(self):
        st = state.default_state()
        cfg = state.get_settings(st)
        self.assertEqual((cfg["priority"], cfg["keepalive"]), ("expiring", False))  # 공개 기본값: 유지 호출 끔
        self.assertEqual(state.set_setting(st, "priority", "Balanced")["priority"], "balanced")
        self.assertFalse(state.set_setting(st, "keepalive", "off")["keepalive"])
        self.assertTrue(state.set_setting(st, "keepalive", True)["keepalive"])
        with self.assertRaises(errors.LimitPilotError):
            state.set_setting(st, "priority", "random")
        with self.assertRaises(errors.LimitPilotError):
            state.set_setting(st, "keepalive", "maybe")
        st["settings"]["keepalive"] = "yes"  # 손상된 값은 기본값
        self.assertFalse(state.get_settings(st)["keepalive"])


class FinalBurnTests(QuietTestCase):
    """주간 리셋 직전(FINAL_BURN_S) — 주간 임계를 넘겨도 최대치까지 남은 한도를 쓴다."""

    def test_active_past_weekly_threshold_keeps_going_near_reset(self):
        st = _state(2)
        st["slots"]["1"]["weeklyThreshold"] = 80
        far = {1: _u(10, 85, weekly_reset_s=2 * 86400), 2: _u(10, 20)}
        self.assertEqual(decide.decide(st, far)[:2], ("switch", 2))         # 평소: 임계 80에서 전환
        near = {1: _u(10, 85, weekly_reset_s=3 * 3600), 2: _u(10, 20)}
        action, _, reason = decide.decide(st, near)
        self.assertEqual(action, "none")                                  # 리셋 3시간 전: 계속 사용
        self.assertIn("주간 85%/100%", reason)
        near_cap = {1: _u(10, 100, weekly_reset_s=3 * 3600), 2: _u(10, 20)}
        self.assertEqual(decide.decide(st, near_cap)[:2], ("switch", 2))     # 최대치에선 전환

    def test_final_burn_goes_past_user_cap_to_100(self):
        # 최대치로 남겨둔 한도도 리셋되면 사라진다 — 리셋 직전엔 100%까지 쓴다
        st = _state(2)
        st["slots"]["1"].update(weeklyCap=70)
        self.assertEqual(decide.decide(st, {1: _u(10, 75, weekly_reset_s=2 * 86400), 2: _u(10, 20)})[:2],
                         ("switch", 2))                                   # 평소: 최대 70에서 전환
        self.assertEqual(decide.decide(st, {1: _u(10, 90, weekly_reset_s=3600), 2: _u(10, 20)})[0],
                         "none")                                          # 리셋 1시간 전: 계속
        lim = limits.slot_limits(st, st["slots"]["1"], _u(10, 90, weekly_reset_s=3600))
        self.assertEqual((lim["cap"], lim["capBase"]), (100.0, 70.0))

    def test_capped_slot_is_probed_again_in_final_window(self):
        st = _state(2)
        st["slots"]["2"].update(weeklyCap=50, usage=_u(0, 55, weekly_reset_s=3600))
        self.assertIsNone(limits.capped_until(st["slots"]["2"], time.time(), st))
        st["slots"]["2"]["usage"] = _u(0, 55, weekly_reset_s=86400)
        self.assertIsNotNone(limits.capped_until(st["slots"]["2"], time.time(), st))

    def test_rebalance_to_account_with_leftover_before_reset(self):
        # 후보는 주간 임계(90)를 넘겼지만 리셋 4시간 전·여유 7%p — 옮겨서 다 쓴다
        st = _state(2)
        st["lastSwitchAt"] = time.time() - 7200
        action, target, reason = decide.decide(st, {
            1: _u(20, 30, weekly_reset_s=5 * 86400), 2: _u(10, 93, weekly_reset_s=4 * 3600)})
        self.assertEqual((action, target), ("switch", 2))
        self.assertIn("여유 7%", reason)
        tiny = {1: _u(20, 30, weekly_reset_s=5 * 86400), 2: _u(10, 97, weekly_reset_s=4 * 3600)}
        self.assertEqual(decide.decide(st, tiny)[0], "none")  # 여유 3%p — 옮길 가치 없음

    def test_final_window_does_not_promote_candidates_far_from_100(self):
        # 리뷰 회귀: 리셋 전 소진 중 주간 84%는 늘어난 한도(100)에서 멀다 — 후보 매 틱 조회 금지
        st = _state(1)
        s1 = st["slots"]["1"]
        self.assertFalse(collector.near_switch(st, s1, _u(10, 84, weekly_reset_s=3600), 90))
        self.assertTrue(collector.near_switch(st, s1, _u(10, 85, weekly_reset_s=3600), 90))
        self.assertFalse(collector.near_switch(st, s1, _u(80, 20, weekly_reset_s=3600), 90))  # 세션도 100까지
        self.assertTrue(collector.near_switch(st, s1, _u(86, 20, weekly_reset_s=3600), 90))
        self.assertTrue(collector.near_switch(st, s1, _u(10, 85, weekly_reset_s=86400), 90))  # 평소 기준
        self.assertIsNone(collector.near_switch(st, s1, None, 90))

    def test_setting_off_restores_threshold(self):
        st = _state(2)
        st["settings"]["finalBurn"] = False
        st["slots"]["1"]["weeklyThreshold"] = 80
        self.assertEqual(decide.decide(st, {1: _u(10, 85, weekly_reset_s=3 * 3600), 2: _u(10, 20)})[:2],
                         ("switch", 2))
        self.assertFalse(state.set_setting(st, "finalBurn", "off")["finalBurn"])

    def test_slot_limits_reports_final_window(self):
        st = _state(1)
        st["slots"]["1"]["weeklyThreshold"] = 80
        lim = limits.slot_limits(st, st["slots"]["1"], _u(10, 85, weekly_reset_s=3600))
        self.assertEqual((lim["weekly"], lim["weeklyBase"], lim["finalBurn"]), (100.0, 80.0, True))
        lim = limits.slot_limits(st, st["slots"]["1"], _u(10, 85, weekly_reset_s=6 * 3600))
        self.assertEqual((lim["weekly"], lim["finalBurn"]), (80.0, False))
        lim = limits.slot_limits(st, st["slots"]["1"],
                             _u(0, 0, weekly_reset_s=None, session_reset_s=None))
        self.assertFalse(lim["finalBurn"])  # 미시작 창은 리셋 시각이 없다


class SessionBurnTests(QuietTestCase):
    """세션 리셋 30분 전 — 세션 임계 무시·최우선 사용(주간 한도·임계를 넘긴 계정은 제외)."""

    def test_active_keeps_using_session_past_threshold_before_reset(self):
        st = _state(2)
        usages = {1: _u(93, 20, session_reset_s=20 * 60), 2: _u(5, 10)}
        action, _, reason = decide.decide(st, usages)
        self.assertEqual(action, "none")
        self.assertIn("세션 93%/100%", reason)
        usages[1] = _u(93, 20, session_reset_s=40 * 60)              # 30분 밖 — 평소대로
        self.assertEqual(decide.decide(st, usages)[:2], ("switch", 2))

    def test_no_session_burn_when_weekly_already_over_threshold(self):
        st = _state(2)
        lim = limits.slot_limits(st, st["slots"]["1"], _u(93, 92, session_reset_s=600))
        self.assertFalse(lim["sessionBurn"])
        self.assertEqual(lim["session"], 90.0)
        st["slots"]["1"]["weeklyCap"] = 60                            # 최대치를 넘긴 경우도 제외
        lim = limits.slot_limits(st, st["slots"]["1"], _u(50, 65, session_reset_s=600))
        self.assertFalse(lim["sessionBurn"])

    def test_burn_target_is_top_priority_and_moves_below_threshold(self):
        st = _state(3)
        st["lastSwitchAt"] = time.time() - 600
        usages = {1: _u(20, 10),
                  2: _u(0, 5, weekly_reset_s=20 * 3600),               # 리셋 임박(24h) 계정
                  3: _u(40, 30, session_reset_s=15 * 60)}             # 세션 15분 뒤 리셋, 여유 60%
        action, target, reason = decide.decide(st, usages)
        self.assertEqual((action, target), ("switch", 3))
        self.assertIn("리셋 직전 소진", reason)
        self.assertIn("세션 15분", reason)

    def test_burn_candidate_over_weekly_threshold_is_skipped(self):
        st = _state(2)
        st["lastSwitchAt"] = time.time() - 600
        self.assertEqual(decide.decide(st, {1: _u(20, 10), 2: _u(40, 91, session_reset_s=600)})[0],
                         "none")

    def test_weekly_final_ignores_session_threshold_too(self):
        st = _state(2)
        st["slots"]["1"]["sessionThreshold"] = 60
        self.assertEqual(decide.decide(st, {1: _u(80, 50, weekly_reset_s=3 * 3600), 2: _u(5, 5)})[0],
                         "none")
        self.assertEqual(decide.decide(st, {1: _u(100, 50, weekly_reset_s=3 * 3600), 2: _u(5, 5)})[:2],
                         ("switch", 2))

    def test_burn_respects_short_cooldown_and_manual_hold(self):
        usages = {1: _u(20, 10), 2: _u(40, 30, session_reset_s=15 * 60)}
        st = _state(2)
        st["lastSwitchAt"] = time.time() - 100
        self.assertEqual(decide.decide(st, usages)[0], "none")
        st = _state(2)
        st["manualChoiceAt"] = time.time() - 600
        self.assertEqual(decide.decide(st, usages)[0], "none")
        st["manualChoiceAt"] = time.time() - constants.BURN_MANUAL_HOLD_S - 1
        self.assertEqual(decide.decide(st, usages)[:2], ("switch", 2))

    def test_session_burning_active_is_not_taken_by_expiring_rebalance(self):
        # 리뷰 회귀: 활성 A가 세션 소진 중이면 B의 주간 리셋 임박 이동이 끼어들지 않는다
        st = _state(2)
        st["lastSwitchAt"] = time.time() - 7200
        usages = {1: _u(60, 30, session_reset_s=20 * 60),
                  2: _u(5, 50, weekly_reset_s=20 * 3600)}
        self.assertEqual(decide.decide(st, usages)[0], "none")

    def test_session_burn_needs_weekly_room_to_threshold(self):
        st = _state(1)
        lim = limits.slot_limits(st, st["slots"]["1"], _u(40, 86, session_reset_s=600))
        self.assertFalse(lim["sessionBurn"])   # 주간 임계 90까지 4%p — 태울 가치 없음
        lim = limits.slot_limits(st, st["slots"]["1"], _u(40, 85, session_reset_s=600))
        self.assertTrue(lim["sessionBurn"])

    def test_active_burning_is_not_moved_away(self):
        st = _state(2)
        usages = {1: _u(50, 10, session_reset_s=20 * 60), 2: _u(40, 30, session_reset_s=10 * 60)}
        self.assertEqual(decide.decide(st, usages)[0], "none")


class KeepaliveTests(QuietTestCase):
    def test_due_interval_and_retry(self):
        now = time.time()
        self.assertTrue(usage_api.keepalive_due({}, now))
        self.assertFalse(usage_api.keepalive_due({"keepalive": {"at": now - 3600, "ok": True}}, now))
        self.assertTrue(usage_api.keepalive_due({"keepalive": {"at": now - 86401, "ok": True}}, now))
        self.assertTrue(usage_api.keepalive_due(
            {"keepalive": {"at": now - 3601, "ok": False, "retry": True}}, now))
        self.assertFalse(usage_api.keepalive_due(
            {"keepalive": {"at": now - 3601, "ok": False, "retry": False}}, now))

    def test_eligibility_excludes_manual_limited_and_quarantined(self):
        self.assertTrue(usage_api.keepalive_eligible({"enabled": False}))
        self.assertFalse(usage_api.keepalive_eligible({"kind": constants.GATEWAY_KIND}))
        self.assertFalse(usage_api.keepalive_eligible(
            {"oauthAccount": {"organizationType": constants.ENTERPRISE_ORG_TYPE}}))
        self.assertFalse(usage_api.keepalive_eligible({"kind": "setup-token"}))
        self.assertFalse(usage_api.keepalive_eligible({"quarantine": {"reason": "x"}}))

    def test_tick_runs_at_most_one_oldest_due_slot(self):
        st = _state(3)
        now = time.time()
        st["slots"]["1"]["keepalive"] = {"at": now - 100, "ok": True}
        st["slots"]["2"]["keepalive"] = {"at": now - 90000, "ok": True}
        with mock.patch.object(collector, "keepalive_slot") as ka:
            tick.tick_keepalive(st, now)
        ka.assert_called_once_with(st, 3, now)  # 기록 없는 슬롯이 가장 오래 쉼

    def test_tick_respects_setting_and_survives_errors(self):
        st = _state(1)
        st["settings"]["keepalive"] = False
        with mock.patch.object(collector, "keepalive_slot") as ka:
            tick.tick_keepalive(st)
        ka.assert_not_called()
        st["settings"]["keepalive"] = True
        with mock.patch.object(collector, "keepalive_slot", side_effect=RuntimeError("boom")):
            tick.tick_keepalive(st)  # 예외가 틱을 죽이지 않는다

    def test_slot_keepalive_records_success(self):
        st = _state(2)
        with (
            mock.patch.object(collector, "maybe_release_quarantine"),
            mock.patch.object(collector, "slot_access_oauth", return_value=({"accessToken": "tok"}, None)),
            mock.patch.object(usage_api, "send_keepalive", return_value=(True, None, None)) as send,
        ):
            rec = collector.keepalive_slot(st, 2, 1000.0)
        send.assert_called_once_with("tok")
        self.assertEqual(rec, {"at": 1000.0, "ok": True, "err": None, "retry": False})
        self.assertIs(st["slots"]["2"]["keepalive"], rec)

    def test_active_expired_token_is_never_refreshed(self):
        st = _state(1)
        st["slots"]["1"]["oauthAccount"] = {"emailAddress": "s1@example.com"}
        with (
            mock.patch.object(accounts, "live_slot_of", return_value=1),
            mock.patch.object(creds, "kc_read_active", return_value="blob"),
            mock.patch.object(oauth, "parse_oauth", return_value={"accessToken": "old"}),
            mock.patch.object(oauth, "is_expired", return_value=True),
            mock.patch.object(oauth, "try_refresh") as refresh,
            mock.patch.object(usage_api, "send_keepalive") as send,
        ):
            rec = collector.keepalive_slot(st, 1)
        refresh.assert_not_called()
        send.assert_not_called()
        self.assertEqual((rec["ok"], rec["err"], rec["retry"]), (False, "token-expired", True))

    def test_inactive_expired_token_is_refreshed_then_pinged(self):
        st = _state(2)
        with (
            mock.patch.object(collector, "maybe_release_quarantine"),
            mock.patch.object(creds, "kc_read_slot", return_value="old-blob"),
            mock.patch.object(oauth, "parse_oauth",
                              side_effect=lambda b: {"accessToken": "new" if b == "new-blob" else "old"}),
            mock.patch.object(oauth, "is_expired", return_value=True),
            mock.patch.object(oauth, "try_refresh", return_value=("new-blob", None)),
            mock.patch.object(creds, "kc_write_slot_retry") as write,
            mock.patch.object(usage_api, "send_keepalive", return_value=(True, None, None)) as send,
        ):
            rec = collector.keepalive_slot(st, 2)
        write.assert_called_once_with(2, "new-blob")
        send.assert_called_once_with("new")
        self.assertTrue(rec["ok"])

    def test_exhausted_slot_only_refreshes_token(self):
        st = _state(2)
        st["slots"]["2"]["usage"] = _u(10, 100, weekly_reset_s=86400)
        with (
            mock.patch.object(collector, "maybe_release_quarantine"),
            mock.patch.object(collector, "slot_access_oauth", return_value=({"accessToken": "tok"}, None)),
            mock.patch.object(usage_api, "send_keepalive") as send,
        ):
            rec = collector.keepalive_slot(st, 2)
        send.assert_not_called()
        self.assertEqual((rec["ok"], rec.get("note")), (True, "refresh-only"))

    def test_failure_classification(self):
        st = _state(2)
        for err, retry in (("http-529", True), ("network", True), ("http-429", True),
                           ("http-403", False), ("http-400", False)):
            with (
                mock.patch.object(collector, "maybe_release_quarantine"),
                mock.patch.object(collector, "slot_access_oauth", return_value=({"accessToken": "t"}, None)),
                mock.patch.object(usage_api, "send_keepalive", return_value=(False, err, "msg")),
            ):
                rec = collector.keepalive_slot(st, 2)
            self.assertEqual((rec["ok"], rec["retry"], rec["detail"]), (False, retry, "msg"), err)

    def test_send_keepalive_request_shape(self):
        captured = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.close()

        def fake_urlopen(req, timeout):
            captured.update(url=req.full_url, body=json.loads(req.data), timeout=timeout,
                            headers={k.lower(): v for k, v in req.header_items()})
            return Resp(b"{}")

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            self.assertEqual(usage_api.send_keepalive("tok"), (True, None, None))
        self.assertEqual(captured["url"], constants.MESSAGES_URL)
        self.assertEqual(captured["body"]["max_tokens"], 1)
        self.assertEqual(captured["body"]["model"], constants.KEEPALIVE_MODEL)
        self.assertEqual(captured["headers"]["authorization"], "Bearer tok")
        self.assertEqual(captured["headers"]["anthropic-beta"], constants.BETA_HEADER)
        self.assertEqual(captured["headers"]["anthropic-version"], constants.ANTHROPIC_VERSION)

    def test_send_keepalive_http_error_extracts_message_and_closes(self):
        body = io.BytesIO(json.dumps({"error": {"message": "nope " * 80}}).encode())
        err = urllib.error.HTTPError(constants.MESSAGES_URL, 403, "Forbidden", {}, body)
        with mock.patch.object(urllib.request, "urlopen", side_effect=err):
            ok, kind, detail = usage_api.send_keepalive("tok")
        self.assertEqual((ok, kind), (False, "http-403"))
        self.assertEqual(len(detail), 160)
        self.assertTrue(body.closed)
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("down")):
            self.assertEqual(usage_api.send_keepalive("tok"), (False, "network", None))


class ManualRefreshTests(QuietTestCase):
    def _run(self, st, n, fetch=(None, None, None), access=({"accessToken": "tok"}, None)):
        with (
            mock.patch.object(collector, "maybe_release_quarantine"),
            mock.patch.object(collector, "slot_access_oauth", return_value=access) as acc,
            mock.patch.object(usage_api, "fetch_usage", return_value=fetch) as f,
        ):
            return collector.refresh_slot_usage(st, n), acc, f

    def test_inactive_slot_refreshes_and_records_snapshot(self):
        st = _state(2)
        u = _u(12, 34)
        (status, _), acc, _ = self._run(st, 2, fetch=(u, None, None))
        self.assertEqual(status, "ok")
        acc.assert_called_once_with(st, 2, allow_refresh=True)
        self.assertEqual(st["slots"]["2"]["usage"], u)
        self.assertAlmostEqual(st["slots"]["2"]["usageFetchedAt"], time.time(), delta=5)

    def test_recent_and_backoff_skip_network(self):
        st = _state(2)
        st["slots"]["2"]["usageFetchedAt"] = time.time() - 5
        (status, _), _, f = self._run(st, 2)
        self.assertEqual(status, "recent")
        f.assert_not_called()
        st["slots"]["2"]["usageFetchedAt"] = time.time() - 600
        st["slots"]["2"]["fetchBackoff"] = {"fails": 1, "until": time.time() + 120}
        (status, until), _, f = self._run(st, 2)
        self.assertEqual(status, "backoff")
        f.assert_not_called()

    def test_failed_attempt_also_throttles(self):
        st = _state(2)
        (status, _), _, _ = self._run(st, 2, access=(None, "refresh-transient"))
        self.assertEqual(status, "error")
        (status, _), acc, _ = self._run(st, 2)
        self.assertEqual(status, "recent-failed")
        acc.assert_not_called()
        self.assertIn("실패", collector.refresh_message(2, status, None))

    def test_429_sets_backoff(self):
        st = _state(2)
        (status, until), _, _ = self._run(st, 2, fetch=(None, "http-429", 300.0))
        self.assertEqual(status, "backoff")
        self.assertAlmostEqual(until - time.time(), 300.0, delta=5)

    def test_credential_error_is_reported(self):
        st = _state(2)
        (status, detail), _, f = self._run(st, 2, access=(None, "quarantined"))
        self.assertEqual((status, detail), ("error", "quarantined"))
        f.assert_not_called()
        self.assertIn("다시 로그인", collector.refresh_message(2, status, detail))

    def test_gateway_and_missing_slots(self):
        st = _state(2)
        st["slots"]["2"]["kind"] = constants.GATEWAY_KIND
        self.assertEqual(collector.refresh_slot_usage(st, 2), ("skip", None))
        with self.assertRaises(errors.LimitPilotError):
            collector.refresh_slot_usage(st, 7)


class DashboardControlTests(QuietTestCase):
    def test_refresh_usage_invalidates_fallback_cache(self):
        st = _state(2)
        status_mod._cache[2] = (time.time(), [], None, None)
        with (
            mock.patch.object(locks, "state_lock", return_value=mock.MagicMock()),
            mock.patch.object(state, "load_state", return_value=st),
            mock.patch.object(accounts, "reconcile_active_slot"),
            mock.patch.object(collector, "refresh_slot_usage", return_value=("ok", None)),
        ):
            result = server.refresh_usage(2)
        self.assertEqual(result["status"], "ok")
        self.assertNotIn(2, status_mod._cache)

    def test_failed_refresh_keeps_fallback_error_backoff(self):
        st = _state(2)
        status_mod._cache[2] = (time.time(), None, None, "http-500")
        with (
            mock.patch.object(locks, "state_lock", return_value=mock.MagicMock()),
            mock.patch.object(state, "load_state", return_value=st),
            mock.patch.object(accounts, "reconcile_active_slot"),
            mock.patch.object(collector, "refresh_slot_usage", return_value=("error", "network")),
        ):
            server.refresh_usage(2)
        self.assertIn(2, status_mod._cache)
        status_mod._cache.pop(2, None)

    def test_apply_settings_validates_types(self):
        st = _state(1)
        with (
            mock.patch.object(locks, "state_lock", return_value=mock.MagicMock()),
            mock.patch.object(state, "load_state", return_value=st),
        ):
            self.assertIn("꺼짐", server.apply_settings({"keepalive": False}))
            self.assertIn("사용률 최저", server.apply_settings({"priority": "balanced"}))
            with self.assertRaises(errors.LimitPilotError):
                server.apply_settings({"keepalive": "off"})

    def test_slot_status_exposes_cap_and_keepalive(self):
        st = _state(2)
        st["slots"]["2"].update(enabled=False, weeklyCap=70,
                                keepalive={"at": 5.0, "ok": False, "err": "http-403",
                                           "detail": "msg", "retry": False})
        out = status_mod.slot_status(st, 2, False)
        self.assertEqual(out["weeklyCap"], 70.0)
        self.assertEqual(out["keepalive"], {"at": 5.0, "ok": False, "err": "http-403",
                                            "note": None, "detail": "msg"})


class StaleBuildReplacementTests(unittest.TestCase):
    """업데이트 뒤 예전 대시보드 서버·데몬 사본이 남아 새 기능이 안 보이던 사고 회귀 방지."""

    def _args(self, **kw):
        return mock.Mock(port=8748, no_open=True, browser="auto", **kw)

    def _main(self, health, stop_result=None):
        with (
            mock.patch.object(server, "purge_in_progress", return_value=False),
            mock.patch.object(deploy_mod, "maybe_refresh_daemon_copy") as sync,
            mock.patch.object(server, "port_in_use", return_value=True),
            mock.patch.object(server, "get_health", return_value=health),
            mock.patch.object(server, "_reuse_running") as reuse,
            mock.patch.object(server, "stop_stale_dashboard",
                              return_value=stop_result) as stop,
            mock.patch.object(server, "ThreadingHTTPServer",
                              side_effect=RuntimeError("server-start")),
            mock.patch("sys.argv", ["dashboard.py", "--no-open"]),
        ):
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = server.main()
            except RuntimeError as e:
                rc = str(e)
        return rc, reuse, stop, sync

    def test_same_build_is_reused(self):
        rc, reuse, stop, sync = self._main({"build": server.BUILD_ID, "clients": 0})
        self.assertEqual(rc, 0)
        reuse.assert_called_once()
        stop.assert_not_called()
        sync.assert_called_once()  # 대시보드 시작은 데몬 사본도 맞춘다

    def test_old_build_without_fingerprint_is_replaced(self):
        rc, reuse, stop, _ = self._main({"version": constants.APP_VERSION, "clients": 1}, stop_result=True)
        self.assertEqual(rc, "server-start")  # 교체 후 새 서버 기동으로 진행
        stop.assert_called_once()
        reuse.assert_not_called()

    def test_failed_replacement_falls_back_to_reuse(self):
        rc, reuse, stop, _ = self._main({"build": "other", "clients": 0}, stop_result=False)
        self.assertEqual(rc, 0)
        reuse.assert_called_once()

    def test_stop_uses_runtime_nonce_then_waits(self):
        records = [("/x", 11, 8748, "n" * 30, "inst-a" * 4), ("/y", 12, 9999, "m" * 30, "i" * 20)]
        with (
            mock.patch.object(runtime, "_dashboard_runtime_records", return_value=records),
            mock.patch.object(urllib.request, "urlopen") as urlopen,
            mock.patch.object(server, "_wait_port_released", return_value=True),
            mock.patch.object(os, "kill") as kill,
        ):
            self.assertTrue(server.stop_stale_dashboard(8748, {"instance": "inst-a" * 4}))
        req = urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "http://127.0.0.1:8748/api/shutdown")
        self.assertEqual(json.loads(req.data), {"nonce": "n" * 30})
        kill.assert_not_called()

    def test_fallback_kills_only_the_recorded_pid_that_owns_the_port(self):
        # 런타임 기록(pid·instance)이 health 의 instance 와 맞고, 그 pid 가 포트 주인일 때만
        records = [("/x", 102, 8748, "n" * 30, "inst-a" * 4)]
        with (
            mock.patch.object(runtime, "_dashboard_runtime_records", return_value=records),
            mock.patch.object(urllib.request, "urlopen", side_effect=OSError("down")),
            mock.patch.object(server, "_port_settled", return_value=False),
            mock.patch.object(server, "_listening_pids", return_value={102, 555}),
            mock.patch.object(server, "_wait_port_released", return_value=True),
            mock.patch.object(os, "kill") as kill,
        ):
            self.assertTrue(server.stop_stale_dashboard(8748, {"instance": "inst-a" * 4}))
        kill.assert_called_once_with(102, signal.SIGTERM)

    def test_pattern_matched_process_without_record_is_never_killed(self):
        # 이름이 비슷한 프로세스(패턴 일치)라도 기록으로 확인되지 않으면 종료하지 않는다
        with (
            mock.patch.object(runtime, "_dashboard_runtime_records", return_value=[]),
            mock.patch.object(runtime, "_dashboard_process_pids", return_value=[101, 102]),
            mock.patch.object(server, "_port_settled", return_value=False),
            mock.patch.object(server, "_listening_pids", return_value={102, 555}),
            mock.patch.object(os, "kill") as kill,
        ):
            self.assertFalse(server.stop_stale_dashboard(8748, {"instance": None}))
        kill.assert_not_called()

    def test_recorded_pid_that_does_not_own_the_port_is_not_killed(self):
        records = [("/x", 102, 8748, "n" * 30, "inst-a" * 4)]
        with (
            mock.patch.object(runtime, "_dashboard_runtime_records", return_value=records),
            mock.patch.object(urllib.request, "urlopen", side_effect=OSError("down")),
            mock.patch.object(server, "_port_settled", return_value=False),
            mock.patch.object(server, "_listening_pids", return_value={555}),
            mock.patch.object(os, "kill") as kill,
        ):
            self.assertFalse(server.stop_stale_dashboard(8748, {"instance": "inst-a" * 4}))
        kill.assert_not_called()

    def test_unknown_listener_is_never_killed(self):
        with (
            mock.patch.object(runtime, "_dashboard_runtime_records", return_value=[]),
            mock.patch.object(runtime, "_dashboard_process_pids", return_value=[101]),
            mock.patch.object(server, "_port_settled", return_value=False),
            mock.patch.object(server, "_listening_pids", return_value={555}),
            mock.patch.object(os, "kill") as kill,
        ):
            self.assertFalse(server.stop_stale_dashboard(8748, {}))
        kill.assert_not_called()

    def test_newer_running_version_is_never_replaced(self):
        rc, reuse, stop, _ = self._main({"version": "99.0.0", "build": "x", "clients": 0})
        self.assertEqual(rc, 0)
        reuse.assert_called_once()
        stop.assert_not_called()

    def test_in_progress_account_add_blocks_replacement(self):
        rc, reuse, stop, _ = self._main({"build": "x", "adding": True, "clients": 1})
        self.assertEqual(rc, 0)
        reuse.assert_called_once()
        stop.assert_not_called()

    def test_port_settled_requires_free_port_or_current_build(self):
        with mock.patch.object(server, "port_in_use", return_value=False):
            self.assertTrue(server._port_settled(8748))
        with (mock.patch.object(server, "port_in_use", return_value=True),
              mock.patch.object(server, "get_health", return_value=None)):
            self.assertFalse(server._port_settled(8748))  # 종료 중(무응답) ≠ 비었음
        with (mock.patch.object(server, "port_in_use", return_value=True),
              mock.patch.object(server, "get_health", return_value={"build": server.BUILD_ID})):
            self.assertTrue(server._port_settled(8748))

    def test_concurrent_launcher_does_not_kill_fresh_server(self):
        with (
            mock.patch.object(runtime, "_dashboard_runtime_records", return_value=[]),
            mock.patch.object(server, "_port_settled", return_value=True),
            mock.patch.object(os, "kill") as kill,
        ):
            self.assertTrue(server.stop_stale_dashboard(8748, {"instance": "a"}))
        kill.assert_not_called()

    def test_windows_netstat_parsing_is_locale_free(self):
        out = ("  TCP    127.0.0.1:8748     0.0.0.0:0      ABHÖREN      4321\n"
               "  TCP    [::1]:8748         [::]:0         LISTENING    4322\n"
               "  TCP    127.0.0.1:18748    0.0.0.0:0      LISTENING    9\n"
               "  TCP    127.0.0.1:8748     127.0.0.1:5000 ESTABLISHED  7\n")
        with (mock.patch.object(sys, "platform", "win32"),
              mock.patch.object(subprocess, "run",
                                return_value=mock.Mock(stdout=out))):
            self.assertEqual(server._listening_pids(8748), {4321, 4322})

    def test_daemon_copy_is_never_downgraded(self):
        import tempfile
        import zipfile

        def fake_pyz(path, version):
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("limitpilot/__init__.py", f'__version__ = "{version}"\n')

        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "limitpilot.pyz")
            fake_pyz(target, "99.0.0")
            with (mock.patch.object(deploy_mod, "deployed_daemon_script_path", return_value=target),
                  mock.patch.object(deploy_mod, "deploy_daemon_script") as deploy,
                  mock.patch.object(logutil, "log")):
                deploy_mod.maybe_refresh_daemon_copy()
                deploy.assert_not_called()
                fake_pyz(target, "0.0.1")
                deploy_mod.maybe_refresh_daemon_copy()
                deploy.assert_called_once()
        self.assertEqual(deploy_mod.version_tuple("1.10.2"), (1, 10, 2))
        self.assertIsNone(deploy_mod.version_tuple("x"))

    def test_build_id_tracks_code_content(self):
        self.assertRegex(server.BUILD_ID, r"^[0-9a-f]{16}$")
        html = server.read_html()
        self.assertIn(server.BUILD_PLACEHOLDER, html)
        self.assertIn(server.TOKEN_PLACEHOLDER, html)
        with mock.patch.object(server, "read_html", side_effect=OSError("missing")):
            self.assertNotEqual(server._compute_build_id(), server.BUILD_ID)

    def test_sync_daemon_command_refreshes_deployed_copy(self):
        with (
            mock.patch.object(deploy_mod, "maybe_refresh_daemon_copy") as sync,
            mock.patch("sys.argv", ["limitpilot.pyz", "sync-daemon"]),
            mock.patch.object(sys, "platform", "darwin"),
        ):
            self.assertEqual(cli.main(), 0)
        sync.assert_called_once()

    def test_widget_and_tray_sync_daemon_on_launch(self):
        root = os.path.join(os.path.dirname(os.path.dirname(server.__file__)), "native")
        for rel in ("macos/main.swift", "windows/tray.cs", "windows/tray.ps1"):
            with open(os.path.join(root, rel), encoding="utf-8-sig") as f:
                self.assertIn("sync-daemon", f.read(), rel)


if __name__ == "__main__":
    unittest.main()
