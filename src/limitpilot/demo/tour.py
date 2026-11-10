"""``limitpilot --demo tour``: watch the real daemon tick switch accounts, in 30 seconds.

Four ticks, 15 virtual minutes apart:

1. account 1 at 62 % — nothing to do; account 3's token cannot be refreshed, so it is
   marked "needs login" and leaves the candidate list,
2. account 1 reaches 91 % (threshold 90 %) — switch to account 2, whose weekly limit
   resets soonest (the "expiring first" rule),
3. and 4. account 2 is now in use; nothing else happens.

Each tick is ``tick.run_tick()`` — the same function the 60-second daemon runs.
"""

import time
from datetime import datetime

from limitpilot import demo
from limitpilot import i18n
from limitpilot import paths
from limitpilot import sandbox
from limitpilot import state
from limitpilot import tick
from limitpilot.demo.clock import VirtualClock
from limitpilot.store import creds

t = i18n.t


def _cell(slot: dict) -> str:
    if not slot:
        return "-"
    if slot.get("quarantine"):
        return "needs login"
    usage = slot.get("usage") or {}
    fh, sd = usage.get("five_hour") or {}, usage.get("seven_day") or {}
    if "pct" not in fh or "pct" not in sd:
        return "?"
    return f"{fh['pct']:.0f}% / {sd['pct']:.0f}%"


def _reason(st: dict, slot_no: int) -> str:
    """Why the active account had to move, from the usage the tick just measured."""
    cfg = state.get_settings(st)
    usage = (state.slot_of(st, slot_no) or {}).get("usage") or {}
    fh, sd = usage.get("five_hour") or {}, usage.get("seven_day") or {}
    if isinstance(fh.get("pct"), (int, float)) and fh["pct"] >= cfg["threshold"]:
        return f"session 5h {fh['pct']:.0f}% >= {cfg['threshold']:.0f}%"
    if isinstance(sd.get("pct"), (int, float)) and sd["pct"] >= cfg["weeklyThreshold"]:
        return f"weekly 7d {sd['pct']:.0f}% >= {cfg['weeklyThreshold']:.0f}%"
    return "rule from policy.decide"


def _hours_to_weekly_reset(st: dict, slot_no: int) -> str:
    usage = (state.slot_of(st, slot_no) or {}).get("usage") or {}
    iso = (usage.get("seven_day") or {}).get("resets_at")
    try:
        at = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return "?"
    return f"{max(0.0, (at - time.time()) / 3600):.0f}"


def _active_token() -> str:
    import json

    blob = creds.kc_read_active() or "{}"
    return (json.loads(blob).get("claudeAiOauth") or {}).get("accessToken", "?")


def run(ticks=None) -> int:
    import os

    start = demo.activate(rebuild=True)   # 투어는 매번 같은 출발점에서 시작한다
    os.environ["LIMITPILOT_QUIET"] = "1"  # 동작 로그는 파일에만 — 화면에는 표만 보여 준다
    scenario = demo.load_scenario()
    ticks = ticks or scenario["ticks"]
    step = scenario["tickMinutes"] * 60
    slots = [acc["slot"] for acc in scenario["accounts"]]
    print(t("tour_title"))
    print(f"  ({paths.STATE_DIR})")
    print(t("tour_header"))
    excluded = set()
    with VirtualClock(start) as clock:
        for i in range(1, ticks + 1):
            before = state.load_state().get("activeSlot")
            tick.run_tick()                    # ← 데몬이 60초마다 실행하는 바로 그 함수
            st = state.load_state()
            after = st.get("activeSlot")
            if after != before:
                action = t("tour_switch", a=before, b=after, reason=_reason(st, before),
                           hours=_hours_to_weekly_reset(st, after))
            else:
                action = t("tour_no_action")
            when = datetime.fromtimestamp(clock.time()).strftime("%H:%M")
            cells = "".join(f"{_cell(state.slot_of(st, n)):<15}" for n in slots)
            print(f"{i:<5} {when:<6} {cells}slot {after!s:<3} {action}")
            for n in slots:
                q = (state.slot_of(st, n) or {}).get("quarantine")
                if q and n not in excluded:
                    excluded.add(n)
                    print("      " + t("tour_excluded", n=n, why=q.get("reason")))
            if after != before:
                print("      " + t("tour_active_now", token=_active_token(), n=after))
            clock.advance(step)
    print(t("tour_real_code"))
    print(sandbox.summary_line())
    return 0
