"""One daemon tick: collect usage → decide → switch (or only notify).

The state lock is held for local work only:

1. lock: read the state and take a snapshot,
2. no lock: ask the usage API for each slot (network; per-slot locks guard token refresh),
3. lock: re-read the state, merge what step 2 learned, decide, switch, save.

When auto-switch is off (the default) a switch decision becomes a "would switch"
notification instead — at most once an hour per target.
"""

import copy
import os
import time
from datetime import datetime

from limitpilot.constants import (EXIT_BLOCKED, EXIT_ERROR, EXIT_NO_ACTION, EXIT_PURGING,
                                  EXIT_SWITCHED, reason_all_exhausted)
from limitpilot.errors import LimitPilotError
from limitpilot import accounts
from limitpilot import collector
from limitpilot import i18n
from limitpilot import locks
from limitpilot import logutil
from limitpilot import notify as notify_mod
from limitpilot import paths
from limitpilot.policy import decide as decide_mod
from limitpilot.policy import limits
from limitpilot import state
from limitpilot import switch


def _stopped() -> bool:
    return any(os.path.exists(os.path.join(paths.STATE_DIR, name))
               for name in (".purging", ".daemon-stopped"))


def tick_keepalive(st: dict, now: float = None) -> None:
    """At most one keep-alive call per tick, only in ticks without a switch.

    Runs on the given state object; callers hold no lock while it does network I/O.
    """
    if not state.get_settings(st)["keepalive"]:
        return
    now = time.time() if now is None else now
    try:
        due = collector.due_keepalive_slots(st, now)
        if due:
            collector.keepalive_slot(st, due[0], now)
    except Exception as e:  # noqa: BLE001 — 유지 호출 실패가 틱을 죽이면 안 된다
        logutil.log(i18n.t("log_keepalive_error", err=type(e).__name__))


def _keepalive_outside_lock() -> None:
    """Keep-alive with the same snapshot → network → merge pattern as the usage fetch."""
    with locks.state_lock():
        if _stopped():
            return
        st = state.load_state()
        if not state.get_settings(st)["keepalive"]:
            return
        if not collector.due_keepalive_slots(st, time.time()):
            return
        accounts.reconcile_active_slot(st)
        base = copy.deepcopy(st)
    work = copy.deepcopy(base)
    tick_keepalive(work)
    with locks.state_lock():
        st = state.load_state()
        collector.merge_changes(st, base, work)
        state.save_state(st)


def run_tick(dry_run: bool = False, carry: dict = None):
    """One check → (exit_code, blocked_reason | None).

    dry-run changes nothing (no switch, no save, no notification). ``carry`` keeps
    ``unhealthyTicks`` between dry-run ticks of the foreground loop so the preview shows
    the same failover decision the daemon would make.
    """
    try:
        # 1) 락 안: 스냅샷만 확보
        with locks.state_lock():
            if _stopped():
                return EXIT_PURGING, None
            st = state.load_state()
            accounts.reconcile_active_slot(st)
            base = copy.deepcopy(st)

        # 2) 락 밖: 네트워크(사용량 조회·비활성 토큰 갱신)
        work = copy.deepcopy(base)
        usages = collector.collect_usage(work, allow_refresh=not dry_run)

        # 3) 락 안: 병합 → 결정 → 전환 → 저장
        run_keepalive = False
        with locks.state_lock():
            if _stopped():
                return EXIT_PURGING, None
            st = state.load_state()
            if dry_run and carry is not None and "unhealthyTicks" in carry:
                st["unhealthyTicks"] = carry["unhealthyTicks"]
            accounts.reconcile_active_slot(st)
            collector.merge_changes(st, base, work)
            if st.get("activeSlot") != work.get("activeSlot"):
                # 조회하는 사이 사용자가 직접 전환했다 — 낡은 측정으로 결정하지 않는다
                logutil.log(i18n.t("log_tick_active_changed"))
                if not dry_run:
                    state.save_state(st)
                return EXIT_NO_ACTION, None
            action, target, reason = decide_mod.decide(st, usages)
            if dry_run and carry is not None:
                carry["unhealthyTicks"] = st.get("unhealthyTicks", 0)
            auto_on = state.get_settings(st)["autoSwitch"]

            if action == "switch":
                email = (state.slot_of(st, target) or {}).get("email")
                if dry_run:
                    switch.perform_switch(st, target, dry_run=True)
                    logutil.log(i18n.t("log_auto_switch", n=target, email=email, reason=reason) + " [dry-run]")
                    return EXIT_SWITCHED, None
                if not auto_on:
                    # 감시·알림만 모드(기본): 전환하지 않고 '전환했을 것' 을 알린다
                    logutil.log(i18n.t("log_would_switch", n=target, email=email, reason=reason))
                    notify_mod.notify_once(
                        st, f"would-switch-{target}", i18n.t("notify_would_title"),
                        i18n.t("notify_would_body", reason=reason, n=target, email=email))
                    state.save_state(st)
                    return EXIT_NO_ACTION, None
                outcome = switch.perform_switch(st, target)
                logutil.log(i18n.t("log_auto_switch", n=target, email=email, reason=reason))
                if outcome == "switched":
                    notify_mod.notify(i18n.t("notify_auto_title"),
                                      i18n.t("notify_auto_body", reason=reason, n=target,
                                             email=email))
                    return EXIT_SWITCHED, None
                state.save_state(st)
                return EXIT_NO_ACTION, None

            if action == "blocked":
                logutil.log(i18n.t("log_blocked", reason=reason))
                if not dry_run:
                    if reason == reason_all_exhausted():
                        reset_t = limits.earliest_reset(
                            [u for u in usages.values() if isinstance(u, dict)])
                        when = (datetime.fromtimestamp(reset_t).astimezone()
                                .strftime("%H:%M") if reset_t else "?")
                        notify_mod.notify_once(st, "all-exhausted",
                                               i18n.t("notify_exhausted_title"),
                                               i18n.t("notify_exhausted_body", when=when))
                    else:
                        notify_mod.notify_once(st, "blocked", i18n.t("notify_blocked_title"),
                                               reason)
                    state.save_state(st)
                    run_keepalive = True
                result = (EXIT_BLOCKED, reason)
            else:
                if not dry_run:
                    state.save_state(st)
                    run_keepalive = True
                logutil.log(i18n.t("log_no_action", reason=reason))
                result = (EXIT_NO_ACTION, None)

        # 4) 락 밖: 전환이 없던 틱에서만 유지 호출(옵트인)
        if run_keepalive:
            _keepalive_outside_lock()
        return result
    except LimitPilotError as e:
        logutil.error(i18n.t("error_prefix", msg=e))
        return EXIT_ERROR, None
    except Exception as e:  # noqa: BLE001
        # 틱 하나의 실패가 auto 루프를 죽이지 않도록 광역 방어(토큰은 예외 메시지에
        # 실리지 않음 — 타입+메시지만 기록)
        logutil.error(i18n.t("log_unexpected", err=f"{type(e).__name__}: {e}"))
        return EXIT_ERROR, None
