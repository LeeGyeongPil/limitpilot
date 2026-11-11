"""Command line interface: ``limitpilot <command>`` / ``python -m limitpilot``.

Exit codes: 0 switched/ok · 1 error · 2 no action · 3 cannot switch · 4 purge in progress.
Heavy modules (dashboard, login, widget, migrate, demo) are imported only by the commands
that need them, so the 60-second daemon tick starts quickly.
"""

import argparse
import os
import sys
import time
from datetime import datetime

from limitpilot import __version__ as APP_VERSION
from limitpilot.constants import (EXIT_BLOCKED, EXIT_ERROR, EXIT_PURGING, FINAL_BURN_S,
                                  INTERVAL_S, LANGUAGES, MAX_SLEEP_S, reason_all_exhausted,
                                  RESET_SLACK_S, SESSION_BURN_S, THRESHOLD_MAX, THRESHOLD_MIN,
                                  TOKEN_EXPIRED, WEEKLY_CAP_MAX, WEEKLY_CAP_MIN)
from limitpilot.errors import LimitPilotError
from limitpilot import accounts
from limitpilot import collector
from limitpilot.daemon import deploy
from limitpilot.daemon import schtasks
from limitpilot import gateway
from limitpilot import i18n
from limitpilot import legacy
from limitpilot import locks
from limitpilot import logutil
from limitpilot import notify as notify_mod
from limitpilot import paths
from limitpilot import sandbox
from limitpilot.policy import decide
from limitpilot.policy import limits
from limitpilot import purge as purge_mod
from limitpilot import state
from limitpilot.store import creds
from limitpilot import switch
from limitpilot import tick
from limitpilot import usage_api

t = i18n.t


def fmt_reset(iso):
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%m/%d %H:%M")
    except ValueError:
        return "?"


def _migrate_banner() -> None:
    """Point users of the previous tool at ``limitpilot migrate`` (never migrates itself)."""
    if legacy.migration_pending():
        print(t("migrate_banner", path=legacy.state_dir()), file=sys.stderr)


# ------------------------------------------------------------------ account commands


def _practice_mode() -> bool:
    """True under ``--demo``: fake accounts, nothing real may be touched."""
    return os.environ.get("LIMITPILOT_DEMO") == "1"


def cmd_login(args) -> int:
    from limitpilot.login import session as session_mod

    if _practice_mode():
        # 연습 모드: 진짜 claude 로그인(브라우저·PTY)을 띄우지 않고 무엇을 할지만 알린다
        print(t("practice_login"))
        return 0
    alias = getattr(args, "alias", None)
    asked_alias = False
    if paths.IS_WIN and not alias:
        # Windows 의 stdin 중계 스레드(readline 블로킹)는 로그인 종료 후에도 다음 한 줄을
        # 삼킬 수 있어, 별칭은 로그인 시작 전에 미리 받는다
        asked_alias = True
        try:
            alias = input(t("ask_alias")).strip() or None
        except EOFError:
            alias = None
    session = session_mod.ClaudeLoginSession()
    try:
        logutil.log(t("login_browser_opens"))
        info = session.start()
        if info.get("url"):
            print(t("login_open_url", url=info["url"]))
        print(t("login_wait"))
        blob, oauth_account = session.run_interactive()
    finally:
        session.close()
    if not alias and not asked_alias:
        try:
            alias = input(t("ask_alias")).strip() or None
        except EOFError:
            alias = None
    with locks.state_lock():
        st = state.load_state()
        n, label, is_new = accounts.register_oauth_slot(st, blob, oauth_account, alias=alias)
    logutil.log(t("slot_registered" if is_new else "slot_updated", n=n, label=label))
    return 0


def cmd_add(args) -> int:
    """Register the account Claude Code is signed in with (the dashboard does the same)."""
    with locks.state_lock():
        st = state.load_state()
        n, email, is_new = accounts.register_current_login(st)
    # 이미 있던 계정이면 '추가'가 아니라 '갱신' — 별칭·계정별 한도는 그대로 남았다고 알린다
    logutil.log(t("slot_added" if is_new else "slot_readded", n=n, email=email))
    return 0


def cmd_gateway_add(args) -> int:
    values = gateway._load_gateway_env(args.env_file)
    base_url = values.get("ANTHROPIC_BASE_URL") or ""
    model = values.get("ANTHROPIC_MODEL") or ""
    auth_mode = values.get("GATEWAY_AUTH_MODE") or "bearer"
    token_key = "ANTHROPIC_API_KEY" if auth_mode.lower() == "api-key" else "ANTHROPIC_AUTH_TOKEN"
    token = values.get(token_key) or ""
    # 빠진 필수 키는 이름으로 알려 준다(값은 절대 출력하지 않는다)
    missing = [key for key, value in (("ANTHROPIC_BASE_URL", base_url), ("ANTHROPIC_MODEL", model),
                                      (token_key, token)) if not value.strip()]
    if missing:
        raise LimitPilotError(t("gw_env_keys_missing", path=os.path.abspath(args.env_file),
                                keys=", ".join(missing)))
    # 별칭을 주지 않으면 None — 이미 있는 게이트웨이면 예전 별칭, 새 게이트웨이면 모델 이름
    alias = args.alias or values.get("GATEWAY_ALIAS") or None
    with locks.state_lock():
        st = state.load_state()
        n, alias, is_new = gateway.register_gateway_slot(
            st, base_url, model, token, auth_mode, alias)
    logutil.log(t("gateway_registered" if is_new else "gateway_updated",
                  n=n, alias=alias, model=model))
    return 0


def cmd_gateway_key(args) -> int:
    """apiKeyHelper entrypoint: stdout is intentionally only the credential."""
    st = state.load_state()
    slot = state.slot_of(st, args.slot)
    if not limits.is_gateway_slot(slot):
        return EXIT_ERROR
    token = creds.kc_read_slot(args.slot)
    if not token:
        return EXIT_ERROR
    sys.stdout.write(token)
    return 0


def cmd_remove(args) -> int:
    with locks.state_lock():
        st = state.load_state()
        accounts.delete_slot(st, args.slot)
    return 0


def cmd_enable(args) -> int:
    with locks.state_lock():
        st = state.load_state()
        accounts.set_slot_enabled(st, args.slot, True)
    return 0


def cmd_disable(args) -> int:
    with locks.state_lock():
        st = state.load_state()
        accounts.set_slot_enabled(st, args.slot, False)
    return 0


# ------------------------------------------------------------------ status commands


def _slot_detail(st, s, u) -> str:
    if s.get("kind") == "setup-token":
        return t("detail_setup_token")
    if limits.is_gateway_slot(s):
        return t("detail_gateway", model=s.get("modelId"), url=s.get("baseUrl"))
    if s.get("quarantine"):
        return t("detail_quarantine", reason=s["quarantine"]["reason"])
    if u == TOKEN_EXPIRED:
        return t("detail_token_expired")
    if isinstance(u, dict) or isinstance(s.get("usage"), dict):
        snap = not isinstance(u, dict)
        uu = u if isinstance(u, dict) else s["usage"]
        sp = uu.get("spend")
        fh, sd = uu.get("five_hour"), uu.get("seven_day")
        if limits.is_enterprise_slot(s) and sp:
            cur = "$" if sp.get("currency") == "USD" else f"{sp.get('currency', '')} "
            return t("detail_enterprise", used=f"{cur}{sp['used']:,.2f}",
                     limit=f"{cur}{sp['limit']:,.2f}", pct=f"{sp['pct']:.1f}")
        if fh and sd:
            return (t("detail_usage", fh=f"{fh['pct']:5.1f}", fh_reset=fmt_reset(fh.get("resets_at")),
                      sd=f"{sd['pct']:5.1f}", sd_reset=fmt_reset(sd.get("resets_at")))
                    + (t("detail_snapshot") if snap else ""))
        return t("detail_partial")
    return t("detail_unknown")


def cmd_list(args) -> int:
    _migrate_banner()
    # 네트워크 조회는 상태 락 밖에서 — 데몬·대시보드가 그동안 기다리지 않는다
    st, usages = collector.collect_and_merge(allow_refresh=True)
    active = st.get("activeSlot")
    regs = state.registered_slots(st)
    cfg = state.get_settings(st)
    print(t("list_header", active=active or t("none_unregistered"), count=len(regs),
            thr=f"{cfg['threshold']:.0f}", priority=cfg["priority"],
            auto=t("on") if cfg["autoSwitch"] else t("off"),
            keepalive=t("on") if cfg["keepalive"] else t("off")))
    if not regs:
        print(t("list_empty"))
    for n in regs:
        s = state.slot_of(st, n)
        mark = t("mark_active") if n == active else ""
        if not s.get("enabled", True):
            mark += " [OFF]"
        lim = limits.slot_limits(st, s, s.get("usage"))
        if lim["sessionOverride"] or lim["weeklyOverride"] or lim["cap"] < WEEKLY_CAP_MAX:
            mark += t("mark_limits", session=f"{lim['session']:.0f}", weekly=f"{lim['weekly']:.0f}",
                      cap=f"{lim['cap']:.0f}")
        if lim["finalBurn"]:
            mark += t("mark_final_burn")
        elif lim["sessionBurn"]:
            mark += t("mark_session_burn")
        detail = _slot_detail(st, s, usages.get(n))
        ka = s.get("keepalive") or {}
        if isinstance(ka.get("at"), (int, float)):
            when = datetime.fromtimestamp(ka["at"]).astimezone().strftime("%m/%d %H:%M")
            res = "OK" if ka.get("ok") else t("failed_with", err=ka.get("err"))
            detail += t("detail_keepalive", when=when, result=res)
        print(t("list_row", n=n, mark=mark, label=state.slot_label(s, n), detail=detail))
    return 0


def cmd_refresh(args) -> int:
    """Manual usage refresh — one short lock per slot so the daemon is never blocked long."""
    with locks.state_lock():
        st = state.load_state()
        targets = [args.slot] if args.slot is not None else state.registered_slots(st)
    rc = 0
    for n in targets:
        with locks.state_lock():
            st = state.load_state()
            accounts.reconcile_active_slot(st)
            if not state.slot_of(st, n):
                logutil.log(t("slot_not_registered", n=n))
                rc = EXIT_ERROR
                continue
            status, detail = collector.refresh_slot_usage(st, n)
            state.save_state(st)
        print(collector.refresh_message(n, status, detail, i18n.current_lang()))
        if status == "error" and args.slot is not None:
            rc = EXIT_ERROR
    return rc


def cmd_limits(args) -> int:
    """Show or set per-account thresholds; the value ``default`` restores the global one."""
    with locks.state_lock():
        st = state.load_state()
        s = state.slot_of(st, args.slot)
        if not s:
            logutil.log(t("slot_not_registered", n=args.slot))
            return EXIT_ERROR
        kw = {k: v for k, v in (("session", args.session), ("weekly", args.weekly),
                                ("cap", args.cap)) if v is not None}
        lim = limits.set_slot_limits(st, args.slot, **kw) if kw else limits.slot_limits(st, s)

    def tag(override):
        return "" if override else t("default_tag")

    print(t("limits_line", n=args.slot, session=f"{lim['session']:.0f}",
            session_tag=tag(lim["sessionOverride"]), weekly=f"{lim['weekly']:.0f}",
            weekly_tag=tag(lim["weeklyOverride"]), cap=f"{lim['cap']:.0f}"))
    return 0


def cmd_cap(args) -> int:
    with locks.state_lock():
        st = state.load_state()
        if args.pct is None:
            s = state.slot_of(st, args.slot)
            if not s:
                logutil.log(t("slot_not_registered", n=args.slot))
                return EXIT_ERROR
            print(f"{limits.weekly_cap(s):.0f}")
            return 0
        limits.set_slot_weekly_cap(st, args.slot, args.pct)
    return 0


def cmd_sync_daemon(args) -> int:
    """Hidden command the widget / tray / dashboard run at start-up.

    ``main()`` refreshes the daemon copy before dispatching, so installing a new app
    alone also updates the daemon. Nothing happens when no daemon is installed.
    """
    return 0


def cmd_keepalive(args) -> int:
    """Run the keep-alive call now (one slot, slots that are due, or all with --force)."""
    now = time.time()
    with locks.state_lock():
        st = state.load_state()
        accounts.reconcile_active_slot(st)
        if args.slot is not None:
            s = state.slot_of(st, args.slot)
            if not s:
                logutil.log(t("slot_not_registered", n=args.slot))
                return EXIT_ERROR
            if not usage_api.keepalive_eligible(s):
                logutil.log(t("keepalive_not_eligible", n=args.slot))
                return EXIT_ERROR
            targets = [args.slot]
        elif args.force:
            targets = [n for n in state.registered_slots(st)
                       if usage_api.keepalive_eligible(state.slot_of(st, n))]
        else:
            targets = collector.due_keepalive_slots(st, now)
    if not targets:
        print(t("keepalive_none_due"))
        return 0
    if _practice_mode():
        # 연습 모드: 대상 고르기(위)는 실제 코드, 보내기만 하지 않는다 — 상태에 실패를 남기지 않는다
        print(t("practice_keepalive", slots=", ".join(str(n) for n in targets)))
        return 0
    rc = 0
    for n in targets:
        with locks.state_lock():
            st = state.load_state()
            accounts.reconcile_active_slot(st)
            if not usage_api.keepalive_eligible(state.slot_of(st, n)):
                continue
            rec = collector.keepalive_slot(st, n)
            state.save_state(st)
        if not rec["ok"]:
            rc = EXIT_ERROR
    return rc


def cmd_switch(args) -> int:
    with locks.state_lock():
        st = state.load_state()
        accounts.reconcile_active_slot(st)
        target = args.slot
        force = bool(getattr(args, "force", False))
        if force and target is None:
            logutil.log(t("switch_force_needs_slot"))
            return EXIT_ERROR
        if target is not None:
            ts = state.slot_of(st, target)
            if not ts:
                logutil.log(t("switch_not_registered", n=target, slots=state.registered_slots(st)))
                return EXIT_ERROR
            if ts.get("kind") == "setup-token":
                logutil.log(t("switch_setup_token", n=target))
                return EXIT_ERROR
            if not ts.get("enabled", True) and not force and not limits.is_manual_only_slot(ts):
                logutil.log(t("switch_slot_off", n=target))
                return EXIT_ERROR
            over = limits.manual_switch_over_cap(st, ts)
            if over is not None:
                over_msg = t("switch_over_cap_forced" if force else "switch_over_cap",
                             n=target, pct=over[0], cap=over[1])
                logutil.log(over_msg)
                if not force:
                    return EXIT_BLOCKED
        if target is None:
            active = st.get("activeSlot")
            if active is None:
                logutil.log(t("switch_no_active"))
                return EXIT_ERROR
            target = decide.best_candidate(st, active)
            if target is None:
                exhausted = decide.collect_candidates(st, active, None, time.time(),
                                                      snapshot_fallback=True)[0]
                logutil.log(t("switch_all_exhausted" if exhausted else "switch_no_candidate"))
                return EXIT_ERROR
        gateway_transition = (limits.is_gateway_slot(state.slot_of(st, target))
                              or st.get("gatewayRestore") is not None)
        st["manualChoiceAt"] = time.time()  # 사용자 선택 — 리셋 임박 선제 이동 유예
        outcome = switch.perform_switch(st, target, force=force)
    target_slot = state.slot_of(st, target) or {}
    email = target_slot.get("email") or state.slot_label(target_slot, target)
    if outcome == "already":
        logutil.log(t("switch_already", n=target, email=email)
                    + (t("switch_gateway_suffix") if gateway_transition else ""))
    else:
        logutil.log(t("switch_done_gateway" if gateway_transition else "switch_done",
                      n=target, email=email))
        notify_mod.notify(t("notify_switched_title"), t("notify_switched_body", n=target, email=email))
    return 0


def cmd_config(args) -> int:
    with locks.state_lock():
        st = state.load_state()
        if args.key is None:
            s = state.get_settings(st)
            print(t("config_threshold", v=f"{s['threshold']:.0f}", lo=f"{THRESHOLD_MIN:.0f}",
                    hi=f"{THRESHOLD_MAX:.0f}"))
            print(t("config_weekly", v=f"{s['weeklyThreshold']:.0f}")
                  + ("" if s["weeklyThresholdSet"] else t("config_weekly_follows")))
            print(t("config_language", v=s["language"], choices=" | ".join(sorted(LANGUAGES))))
            print(t("config_auto_switch", v=t("on") if s["autoSwitch"] else t("off")))
            print(t("config_keepalive", v=t("on") if s["keepalive"] else t("off")))
            print(t("config_final_burn", v=t("on") if s["finalBurn"] else t("off"),
                    hours=f"{FINAL_BURN_S / 3600:.0f}", mins=f"{SESSION_BURN_S / 60:.0f}"))
            print(t("config_priority", v=s["priority"]))
            return 0
        aliases = {"weekly-threshold": "weeklyThreshold", "weekly_threshold": "weeklyThreshold",
                   "final-burn": "finalBurn", "final_burn": "finalBurn",
                   "auto-switch": "autoSwitch", "auto_switch": "autoSwitch"}
        key = aliases.get(args.key, args.key)
        key = key if key in ("threshold", "language", "keepalive", "priority", "weeklyThreshold",
                             "finalBurn", "autoSwitch") else None
        if not key:
            logutil.log(t("config_unknown", key=args.key))
            return EXIT_ERROR
        if args.value is None:
            v = state.get_settings(st)[key]
            print(("on" if v else "off") if isinstance(v, bool) else v)
            return 0
        if key == "weeklyThreshold" and args.value.strip().lower() == "default":
            args.value = None
        s = state.set_setting(st, key, args.value)
        logutil.log(t("config_updated", key=key, value=s[key]))
    return 0


def _explain_stopped() -> None:
    """Tell a manual `auto` run why it ends at once (stop marker or purge in progress)."""
    if os.environ.get("LIMITPILOT_DAEMON") == "1":
        return  # 데몬 틱은 조용히 끝난다 — stderr 로그를 늘리지 않는다
    if os.path.exists(os.path.join(paths.STATE_DIR, ".purging")):
        print(t("watcher_purging"), file=sys.stderr)
    else:
        print(t("watcher_stopped"), file=sys.stderr)


def cmd_auto(args) -> int:
    if args.once:
        rc, _ = tick.run_tick(dry_run=args.dry_run)
        if rc == EXIT_PURGING:
            _explain_stopped()
        return rc
    if tick._stopped():
        _explain_stopped()  # 'Watching started' 를 찍고 조용히 끝나는 혼동을 막는다
        return 0
    thr = state.get_settings(state.load_state())["threshold"]
    logutil.log(t("auto_loop_start", thr=f"{thr:.0f}", interval=f"{INTERVAL_S:.0f}")
                + (" (dry-run)" if args.dry_run else ""))
    carry = {} if args.dry_run else None
    while True:
        rc, blocked_reason = tick.run_tick(dry_run=args.dry_run, carry=carry)
        if rc == EXIT_PURGING:
            _explain_stopped()
            return 0
        delay = INTERVAL_S
        # 오래 쉬는 것은 진짜 소진 상태에서만 — 미등록·격리 차단은 정상 주기를 유지
        if rc == EXIT_BLOCKED and blocked_reason == reason_all_exhausted():
            st = state.load_state()
            usages = [(state.slot_of(st, n) or {}).get("usage") for n in state.registered_slots(st)]
            reset_t = limits.earliest_reset([u for u in usages if u])
            if reset_t:
                delay = min(max(reset_t + RESET_SLACK_S - time.time(), INTERVAL_S),
                            MAX_SLEEP_S)
        time.sleep(delay)


# ------------------------------------------------------------------ daemon commands


def cmd_install_daemon(args) -> int:
    from limitpilot.daemon import launchd

    if _practice_mode():
        # 연습 모드: 실제 launchctl·schtasks 를 부르지 않고 표식 파일도 건드리지 않는다
        if getattr(args, "enable_auto", False):
            with locks.state_lock():
                st = state.load_state()
                state.set_setting(st, "autoSwitch", True)
        print(t("practice_daemon_install",
                auto=t("practice_daemon_auto") if getattr(args, "enable_auto", False) else ""))
        return 0

    os.makedirs(paths.STATE_DIR, mode=0o700, exist_ok=True)
    if os.path.exists(os.path.join(paths.STATE_DIR, ".purging")):
        raise LimitPilotError(t("purge_unfinished"))
    # 이전 도구의 데몬이 살아 있으면 두 데몬이 같은 자격증명을 놓고 경쟁한다 — 경고만
    for warning in legacy.running_legacy_daemons():
        print(t("legacy_daemon_warning", what=warning), file=sys.stderr)
    try:
        os.remove(os.path.join(paths.STATE_DIR, ".daemon-stopped"))
    except FileNotFoundError:
        pass
    except OSError as e:
        raise LimitPilotError(t("daemon_marker_failed")) from e
    if getattr(args, "enable_auto", False):
        # 자동 전환은 명시적 옵트인 — 이 플래그 없이 설치하면 감시·알림만 한다
        with locks.state_lock():
            st = state.load_state()
            state.set_setting(st, "autoSwitch", True)
    daemon_script = deploy.deploy_daemon_script()
    if paths.IS_WIN:
        rc = schtasks._win_install_daemon(daemon_script)
    else:
        rc = launchd.install(deploy.find_python(), daemon_script)
    if rc == 0:
        auto_on = state.get_settings(state.load_state())["autoSwitch"]
        print(t("daemon_installed_auto" if auto_on else "daemon_installed_watch"))
    return rc


def cmd_uninstall_daemon(args) -> int:
    if _practice_mode():
        print(t("practice_daemon_uninstall"))  # 연습 모드: 표식·launchctl 모두 건드리지 않는다
        return 0
    # 종료 UI 가 성공으로 돌아와도 스케줄러가 남는 거짓 성공을 막는다.
    os.makedirs(paths.STATE_DIR, mode=0o700, exist_ok=True)
    marker = os.path.join(paths.STATE_DIR, ".daemon-stopped")
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
    except OSError as e:
        raise LimitPilotError(t("daemon_stop_marker_failed")) from e
    # 이미 전환 중인 틱을 중간에 끊지 않고, 락을 잡은 뒤에만 /End·bootout 을 실행한다.
    # 대기 중인 새 틱은 표식을 보고 바로 끝난다.
    with locks.state_lock():
        deploy._stop_daemon(strict=True)
    logutil.log(t("daemon_removed",
                  name=paths.WIN_TASK_NAME if paths.IS_WIN else paths.LAUNCHD_LABEL))
    return 0


# ------------------------------------------------------------------ new in limitpilot


def cmd_dashboard(args) -> int:
    from limitpilot.dashboard import server

    argv = ["--port", str(args.port or paths.DASHBOARD_DEFAULT_PORT), "--browser", args.browser]
    if args.no_open:
        argv.append("--no-open")
    if args.add:
        argv.append("--add")
    if args.app_browser:
        argv += ["--app-browser", args.app_browser]
    return server.main(argv)


def cmd_migrate(args) -> int:
    from limitpilot import migrate

    return migrate.run(dry_run=args.dry_run, purge_legacy=args.purge_legacy)


def cmd_doctor(args) -> int:
    from limitpilot import doctor

    return doctor.run()


def cmd_widget(args) -> int:
    from limitpilot import widget

    return widget.run(args)


def cmd_tour(args) -> int:
    from limitpilot.demo import tour

    return tour.run()


# --------------------------------------------------------------------- main


def h(key: str) -> str:
    """Help text for argparse (a literal % must be written %%)."""
    return t(key).replace("%", "%%")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="limitpilot", description=h("cli_description"))
    p.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    p.add_argument("--demo", action="store_true", help=h("help_demo"))
    p.add_argument("--fresh", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--lang", choices=sorted(LANGUAGES), help=h("help_lang"))
    p.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    lg = sub.add_parser("login", help=h("help_login"))
    lg.add_argument("--alias", help=h("help_alias"))
    sub.add_parser("add", help=h("help_add"))
    ga = sub.add_parser("gateway-add", help=h("help_gateway_add"))
    ga.add_argument("--env-file", default="gateway.local.env", help=h("help_env_file"))
    ga.add_argument("--alias", help=h("help_alias"))
    gk = sub.add_parser("gateway-key")   # 숨김: help 를 주지 않으면 목록에 안 나온다
    gk.add_argument("slot", type=int)
    rm = sub.add_parser("remove", help=h("help_remove"))
    rm.add_argument("slot", type=int)
    en = sub.add_parser("enable", help=h("help_enable"))
    en.add_argument("slot", type=int)
    di = sub.add_parser("disable", help=h("help_disable"))
    di.add_argument("slot", type=int)
    cf = sub.add_parser("config", help=h("help_config"))
    cf.add_argument("key", nargs="?", help=h("help_config_key"))
    cf.add_argument("value", nargs="?", help=h("help_config_value"))
    lm = sub.add_parser("limits", help=h("help_limits"))
    lm.add_argument("slot", type=int)
    lm.add_argument("--session", help=h("help_limits_session"))
    lm.add_argument("--weekly", help=h("help_limits_weekly"))
    lm.add_argument("--cap", help=h("help_limits_cap"))
    cp = sub.add_parser("cap", help=h("help_cap"))
    cp.add_argument("slot", type=int)
    cp.add_argument("pct", nargs="?", type=float,
                    help=f"{WEEKLY_CAP_MIN:.0f}~{WEEKLY_CAP_MAX:.0f}")
    rf = sub.add_parser("refresh", help=h("help_refresh"))
    rf.add_argument("slot", type=int, nargs="?")
    ka = sub.add_parser("keepalive", help=h("help_keepalive"))
    ka.add_argument("slot", type=int, nargs="?")
    ka.add_argument("--force", action="store_true", help=h("help_keepalive_force"))
    sub.add_parser("sync-daemon")        # 숨김(위젯·트레이·대시보드가 시작할 때 실행)
    sub.add_parser("list", help=h("help_list"))
    sub.add_parser("status", help=h("help_status"))
    sw = sub.add_parser("switch", help=h("help_switch"))
    sw.add_argument("slot", type=int, nargs="?")
    sw.add_argument("--force", action="store_true", help=h("help_switch_force"))
    au = sub.add_parser("auto", help=h("help_auto"))
    au.add_argument("--once", action="store_true", help=h("help_auto_once"))
    au.add_argument("--dry-run", action="store_true", help=h("help_auto_dry_run"))
    ins = sub.add_parser("install-daemon", help=h("help_install_daemon"))
    ins.add_argument("--enable-auto", action="store_true", help=h("help_enable_auto"))
    sub.add_parser("uninstall-daemon", help=h("help_uninstall_daemon"))
    purge = sub.add_parser("purge", help=h("help_purge"))
    purge.add_argument("--yes", action="store_true", help=h("help_purge_yes"))
    db = sub.add_parser("dashboard", help=h("help_dashboard"))
    db.add_argument("--port", type=_port_arg, help=h("help_dashboard_port"))
    db.add_argument("--no-open", action="store_true", help=h("help_dashboard_no_open"))
    db.add_argument("--browser", choices=["auto", "chrome", "default"], default="auto")
    db.add_argument("--app-browser", help=h("help_dashboard_app_browser"))
    db.add_argument("--add", action="store_true", help=h("help_dashboard_add"))
    mg = sub.add_parser("migrate", help=h("help_migrate"))
    mg.add_argument("--dry-run", action="store_true", help=h("help_migrate_dry_run"))
    mg.add_argument("--purge-legacy", action="store_true", help=h("help_migrate_purge"))
    sub.add_parser("doctor", help=h("help_doctor"))
    wd = sub.add_parser("widget", help=h("help_widget"))
    wd.add_argument("action", choices=["build", "install", "uninstall"])
    wd.add_argument("--out", help=h("help_widget_out"))
    wd.add_argument("--dry-run", action="store_true", help=h("help_widget_dry_run"))
    sub.add_parser("tour", help=h("help_tour"))
    return p


def _port_arg(value: str) -> int:
    """argparse type: a TCP port 1-65535 (friendly error instead of a traceback)."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(t("port_out_of_range", value=value))
    return port


def _early_lang(argv) -> None:
    """Apply --lang before the parser is built so --help is translated too."""
    for i, a in enumerate(argv):
        if a == "--lang" and i + 1 < len(argv):
            i18n.set_lang(argv[i + 1])
        elif a.startswith("--lang="):
            i18n.set_lang(a.split("=", 1)[1])


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _early_lang(argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.daemon:
        os.environ["LIMITPILOT_DAEMON"] = "1"
        logutil.rotate_daemon_err_log()
    if args.cmd is None:
        parser.print_help()
        return EXIT_ERROR
    if args.demo or args.cmd == "tour":
        # 연습 모드: 임시 폴더의 가짜 저장소·가짜 Claude Code 파일·가상 사용량을 쓴다
        from limitpilot import demo

        demo.activate(fresh=args.fresh)
    elif sys.platform not in ("darwin", "win32") and args.cmd not in ("doctor", "widget"):
        logutil.log(t("unsupported_platform"))
        return EXIT_ERROR
    if args.cmd not in ("purge", "uninstall-daemon", "gateway-key", "tour", "migrate") \
            and not args.demo:
        deploy.maybe_refresh_daemon_copy()
    dispatch = {
        "login": cmd_login, "add": cmd_add, "gateway-add": cmd_gateway_add,
        "gateway-key": cmd_gateway_key, "remove": cmd_remove,
        "enable": cmd_enable, "disable": cmd_disable,
        "config": cmd_config, "cap": cmd_cap, "limits": cmd_limits, "refresh": cmd_refresh,
        "keepalive": cmd_keepalive, "sync-daemon": cmd_sync_daemon,
        "list": cmd_list, "status": cmd_list,
        "switch": cmd_switch, "auto": cmd_auto,
        "install-daemon": cmd_install_daemon,
        "uninstall-daemon": cmd_uninstall_daemon,
        "purge": purge_mod.cmd_purge,
        "dashboard": cmd_dashboard, "migrate": cmd_migrate, "doctor": cmd_doctor,
        "widget": cmd_widget, "tour": cmd_tour,
    }
    try:
        return dispatch[args.cmd](args)
    except LimitPilotError as e:
        if args.cmd == "purge":
            # purge 뒤에 log() 를 부르면 지운 상태 폴더가 다시 생긴다.
            print(t("error_prefix", msg=e), file=sys.stderr)
            if os.path.exists(os.path.join(paths.STATE_DIR, ".purging")):
                print(t("purge_partial"), file=sys.stderr)
        else:
            logutil.error(t("error_prefix", msg=e))
        return EXIT_ERROR
    except sandbox.SandboxViolation as e:
        print(t("error_prefix", msg=t("sandbox_blocked", detail=e)), file=sys.stderr)
        return EXIT_ERROR
    except Exception as e:  # noqa: BLE001 - 최후 방어선: 트레이스백 대신 한 줄
        if os.environ.get("LIMITPILOT_DEBUG") == "1":
            raise
        print(t("error_prefix", msg=t("unexpected_error", err=f"{type(e).__name__}: {e}")),
              file=sys.stderr)
        return EXIT_ERROR
