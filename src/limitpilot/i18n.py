"""English / Korean messages shared by the CLI, notifications, the demo and the dashboard.

Language choice, first match wins:
``--lang`` → ``LIMITPILOT_LANG`` → OS locale (``ko*`` → ko) → ``en``.
The widget, tray and dashboard follow ``settings.language`` in the state file, whose
default is the same OS-locale guess.
"""

import locale
import os

LANGS = ("en", "ko")
_override = None

# key: (English, Korean). 두 언어가 같은 키·같은 자리표시자를 갖도록 한 표로 관리한다.
_PAIRS = {
    # 공통
    "on": ("on", "켜짐"),
    "off": ("off", "꺼짐"),
    "error_prefix": ("Error: {msg}", "오류: {msg}"),
    "failed_with": ("failed ({err})", "실패({err})"),
    "slot_n": ("Slot {n}", "슬롯 {n}"),
    "unsupported_platform": ("LimitPilot runs on macOS and Windows only",
                             "macOS/Windows 전용 도구입니다"),
    "migrate_banner": (
        "Found data from claude-account-pilot ({path}). "
        "Run `limitpilot migrate --dry-run` to see the steps, then `limitpilot migrate`.",
        "claude-account-pilot 데이터({path})를 찾았습니다. "
        "`limitpilot migrate --dry-run` 으로 단계를 확인한 뒤 `limitpilot migrate` 를 실행하세요."),
    "cli_description": (
        "See Claude Code usage per account and switch accounts before a limit hits "
        "(macOS / Windows).",
        "Claude Code 계정별 사용량을 보고 한도 전에 계정을 전환합니다 (macOS / Windows)."),
    # 계정 등록
    "ask_alias": ("Alias (display name, optional) > ", "별칭(표시명, 생략 가능) > "),
    "login_browser_opens": (
        "A browser window opens — sign in with the account to add and approve.",
        "브라우저가 열립니다 — 추가할 계정으로 로그인 후 '승인'하세요..."),
    "login_open_url": ("\nIf no browser opened, visit:\n  {url}\n",
                       "\n브라우저가 안 열리면 이 주소로 접속:\n  {url}\n"),
    "login_wait": ("It finishes by itself after you approve. (Paste the code here if one is shown.)",
                   "로그인·승인하면 자동으로 완료됩니다. (코드가 표시되면 여기에 붙여넣으세요)"),
    "slot_registered": ("Slot {n} added: {label} — active account unchanged",
                        "슬롯 {n} 등록: {label} — 활성 계정 무변경"),
    "slot_updated": ("Slot {n} updated: {label} — active account unchanged",
                     "슬롯 {n} 갱신: {label} — 활성 계정 무변경"),
    "slot_added": ("Slot {n} added: {email}", "슬롯 {n} 등록: {email}"),
    "slot_readded": (
        "Slot {n} updated: {email} - signed in again; alias and per-account limits kept",
        "슬롯 {n} 갱신: {email} — 다시 로그인한 계정, 별칭·계정별 한도는 그대로"),
    "add_no_login": ("No Claude account is signed in — run /login in Claude Code, then retry",
                     "현재 로그인된 Claude 계정이 없습니다 — Claude Code에서 /login 후 재실행"),
    "add_api_key": ("API-key credentials are not supported (subscription OAuth only)",
                    "API 키 자격증명은 지원하지 않습니다(구독 OAuth 전용)"),
    "add_bad_format": ("The active credential format is not recognised (no claudeAiOauth)",
                       "활성 자격증명 형식을 인식할 수 없습니다(claudeAiOauth 없음)"),
    "add_no_account": ("No account info (oauthAccount) in ~/.claude.json",
                       "~/.claude.json에서 계정정보(oauthAccount)를 찾을 수 없습니다"),
    "gateway_registered": ("Slot {n} gateway added: {alias} ({model})",
                           "슬롯 {n} 게이트웨이 등록: {alias} ({model})"),
    "gateway_updated": ("Slot {n} gateway updated: {alias} ({model})",
                        "슬롯 {n} 게이트웨이 갱신: {alias} ({model})"),
    "slot_not_registered": ("Slot {n} is not registered", "슬롯 {n}은 등록되어 있지 않습니다"),
    # list
    "list_header": (
        "Active slot: {active} · {count} account(s) · threshold {thr}% · priority {priority}"
        " · auto-switch {auto} · keep-alive {keepalive}",
        "활성 슬롯: {active} · 등록 계정 {count}개 · 전환 임계 {thr}% · 우선순위 {priority}"
        " · 자동 전환 {auto} · 유지 호출 {keepalive}"),
    "none_unregistered": ("none (unregistered account)", "없음(미등록 계정)"),
    "list_empty": ("  No accounts yet — sign in to Claude Code and run `limitpilot add`",
                   "  등록된 계정이 없습니다 — 로그인 후 `limitpilot add` 를 실행하세요"),
    "list_row": ("  Slot {n}{mark}: {label}  {detail}", "  슬롯 {n}{mark}: {label}  {detail}"),
    "mark_active": (" (active)", " (active)"),
    "mark_limits": (" [limits session {session}·weekly {weekly}·max {cap}%]",
                    " [임계 세션 {session}·주간 {weekly}·최대 {cap}%]"),
    "mark_final_burn": (" [use up before weekly reset]", " [주간 리셋 전 소진]"),
    "mark_session_burn": (" [use up before session reset]", " [세션 리셋 전 소진]"),
    "detail_setup_token": ("[limited token — remove it and add the account again in the browser]",
                           "[제한 토큰 — 삭제 후 브라우저로 다시 추가 필요]"),
    "detail_gateway": ("third-party gateway · {model} · {url} · manual only",
                       "서드파티 게이트웨이 · {model} · {url} · 수동 전용"),
    "detail_quarantine": ("[needs login: {reason}]", "[다시 로그인 필요: {reason}]"),
    "detail_token_expired": ("[token expired — Claude Code renews it on next use]",
                             "[토큰 만료 — Claude Code 사용 시 자동 갱신됨]"),
    "detail_enterprise": ("enterprise · used {used} / {limit} ({pct}%) · manual only",
                          "엔터프라이즈 · 사용 {used} / {limit} ({pct}%) · 수동 전용"),
    "detail_usage": ("5h {fh}% (resets {fh_reset})  7d {sd}% (resets {sd_reset})",
                     "5h {fh}% (리셋 {fh_reset})  7d {sd}% (리셋 {sd_reset})"),
    "detail_snapshot": (" [snapshot]", " [스냅샷]"),
    "detail_partial": ("[partial usage]", "[사용량 부분 조회]"),
    "detail_unknown": ("[usage unknown]", "[사용량 불명]"),
    "detail_keepalive": ("  · keep-alive {when} {result}", "  · 유지 호출 {when} {result}"),
    "limits_line": (
        "Slot {n}: session threshold {session}%{session_tag} · weekly threshold {weekly}%"
        "{weekly_tag} · weekly max {cap}%",
        "슬롯 {n}: 세션 임계 {session}%{session_tag} · 주간 임계 {weekly}%{weekly_tag}"
        " · 주간 최대 {cap}%"),
    "default_tag": ("(default)", "(기본)"),
    # keepalive
    "keepalive_not_eligible": (
        "Slot {n} cannot use keep-alive (gateway, enterprise, limited token or needs login)",
        "슬롯 {n}은 유지 호출 대상이 아닙니다(게이트웨이·엔터프라이즈·제한 토큰·격리)"),
    "keepalive_none_due": ("No account needs a keep-alive call (all done within 24 h)",
                           "유지 호출이 필요한 계정이 없습니다(모두 24시간 안에 완료)"),
    # switch
    "switch_force_needs_slot": ("--force needs a slot number", "강제 선택은 슬롯 번호를 같이 지정해야 합니다"),
    "switch_not_registered": ("Slot {n} is not registered (registered: {slots})",
                              "슬롯 {n}은 등록되어 있지 않습니다 (등록: {slots})"),
    "switch_setup_token": ("Slot {n} is a limited token — remove it and add it again in the browser",
                           "슬롯 {n}은 제한 토큰입니다 — 삭제 후 브라우저로 다시 추가하세요"),
    "switch_slot_off": ("Slot {n} is OFF — run `enable {n}` first or use `switch {n} --force`",
                        "슬롯 {n}은 OFF 상태입니다 — `enable {n}` 후 전환하거나 "
                        "`switch {n} --force`를 사용하세요"),
    "switch_no_active": ("The active slot is unknown — pick one with `switch <slot>`",
                         "활성 슬롯을 알 수 없습니다 — `switch <슬롯번호>`로 지정하세요"),
    "switch_no_candidate": ("No other account to switch to — sign in with another account and run `add`",
                            "전환할 다른 계정이 없습니다 — 다른 계정 로그인 후 `add`로 등록하세요"),
    "switch_all_exhausted": ("Every other account is at its limit or over its weekly max — nothing to switch to. "
                             "Name a slot (`switch <slot>`) to pick one anyway",
                             "다른 계정이 모두 한도 소진이거나 주간 최대치를 넘었습니다 — 전환할 곳이 없습니다. "
                             "그래도 옮기려면 슬롯 번호를 지정하세요(`switch <슬롯번호>`)"),
    "switch_already": ("Slot {n} ({email}) is already active", "이미 슬롯 {n}({email})이 활성입니다"),
    "switch_gateway_suffix": (" — running sessions pick it up shortly",
                              " — 실행 중 세션에는 잠시 후 반영"),
    "switch_done": ("Switched to slot {n} ({email}) — running sessions follow within ~30 s "
                    "(restart Claude Code to apply at once)",
                    "슬롯 {n}({email})로 전환 완료 — 실행 중 세션은 ~30초 내 반영"
                    "(즉시 적용하려면 Claude Code/확장 재시작)"),
    "switch_done_gateway": ("Switched to slot {n} ({email}) — running sessions pick it up shortly "
                            "(start a new chat only if it does not)",
                            "슬롯 {n}({email})로 전환 완료 — 실행 중 세션에는 잠시 후 반영"
                            "(오래 반영되지 않을 때만 새 채팅 또는 재시작)"),
    # notifications
    "notify_switched_title": ("LimitPilot: account switched", "LimitPilot: 계정 전환"),
    "notify_switched_body": ("Slot {n} ({email}) is now active", "슬롯 {n} ({email}) 활성화"),
    "notify_auto_title": ("LimitPilot: switched automatically", "LimitPilot: 자동 전환"),
    "notify_auto_body": ("{reason} → slot {n} ({email})", "{reason} → 슬롯 {n} ({email})"),
    "notify_would_title": ("LimitPilot: would switch", "LimitPilot: 전환 제안"),
    "notify_would_body": (
        "{reason} → slot {n} ({email}). Auto-switch is off — run `limitpilot switch {n}` "
        "or turn it on with `limitpilot config auto-switch on`.",
        "{reason} → 슬롯 {n} ({email}). 자동 전환이 꺼져 있습니다 — `limitpilot switch {n}` "
        "또는 `limitpilot config auto-switch on`."),
    "notify_exhausted_title": ("LimitPilot: all accounts at limit", "LimitPilot: 모든 계정 한도"),
    "notify_exhausted_body": ("All accounts reached their limit — next reset {when}",
                              "모든 계정 한도 초과 — {when} 리셋 대기"),
    "notify_blocked_title": ("LimitPilot: cannot switch", "LimitPilot: 전환 불가"),
    "notify_quarantine_title": ("LimitPilot: account needs login", "LimitPilot: 다시 로그인 필요"),
    "notify_quarantine_body": ("Slot {n} ({email}) token expired — sign in again",
                               "슬롯 {n}({email}) 토큰 만료 — 재로그인 필요"),
    "notify_rollback_title": ("LimitPilot: switch rollback failed", "LimitPilot: 전환 원복 실패"),
    # config
    "config_threshold": ("threshold        = {v}%  (session threshold, global, {lo}~{hi})",
                         "threshold        = {v}%  (세션 임계 — 전역, {lo}~{hi})"),
    "config_weekly": ("weekly-threshold = {v}%  (weekly threshold, global)",
                      "weekly-threshold = {v}%  (주간 임계 — 전역)"),
    "config_weekly_follows": (" (follows the session threshold — set `default` to reset)",
                              " (세션 임계를 따름 — default 로 되돌림)"),
    "config_language": ("language         = {v}  (widget / dashboard language, {choices})",
                        "language         = {v}  (위젯/대시보드 언어, {choices})"),
    "config_auto_switch": ("auto-switch      = {v}  (daemon switches accounts by itself; off = notify only)",
                           "auto-switch      = {v}  (데몬이 직접 전환; 꺼짐 = 알림만)"),
    "config_keepalive": ("keepalive        = {v}  (one minimal request per account per day; counts as usage)",
                         "keepalive        = {v}  (계정별 하루 1회 최소 요청; 사용량에 잡힘)"),
    "config_final_burn": ("final-burn       = {v}  (use accounts up to 100% {hours} h before weekly / "
                          "{mins} min before session reset)",
                          "final-burn       = {v}  (주간 리셋 {hours}시간 전·세션 리셋 {mins}분 전 "
                          "계정을 임계 무시·최우선으로 소진)"),
    "config_priority": ("priority         = {v}  (expiring: weekly reset soonest first | "
                        "balanced: lowest usage first)",
                        "priority         = {v}  (expiring: 주간 리셋 임박 여유분 우선 | "
                        "balanced: 사용률 최저 우선)"),
    "config_unknown": ("Unknown setting: {key} (threshold | weekly-threshold | auto-switch | "
                       "final-burn | language | keepalive | priority)",
                       "알 수 없는 설정: {key} (threshold | weekly-threshold | auto-switch | "
                       "final-burn | language | keepalive | priority)"),
    "config_updated": ("Setting updated: {key} = {value}", "설정 갱신: {key} = {value}"),
    "auto_loop_start": ("Watching started (threshold {thr}%, every {interval}s)",
                        "감시 시작 (임계 {thr}%, 주기 {interval}초)"),
    # daemon
    "purge_unfinished": ("A previous purge did not finish — run `purge --yes` again",
                         "이전 완전 삭제가 끝나지 않았습니다 — `purge --yes`를 다시 실행하세요"),
    "purge_partial": ("Auto-switch was stopped for safety after a partial purge. "
                      "Fix the problem, then run `purge --yes` again.",
                      "부분 삭제 후 자동 전환은 안전을 위해 중지됐습니다. "
                      "문제를 해결한 뒤 `purge --yes`를 다시 실행하세요."),
    "legacy_daemon_warning": (
        "Warning: the claude-account-pilot daemon is still installed ({what}). Two daemons "
        "would fight over the same credentials — run `limitpilot migrate` first.",
        "경고: claude-account-pilot 데몬이 아직 설치돼 있습니다({what}). 두 데몬이 같은 "
        "자격증명을 두고 경쟁합니다 — 먼저 `limitpilot migrate` 를 실행하세요."),
    "daemon_marker_failed": ("Could not clear the stop marker", "감시 중지 표식을 해제하지 못했습니다"),
    "daemon_stop_marker_failed": ("Could not create the stop marker",
                                  "감시 중지 안전 표식을 생성하지 못했습니다"),
    "daemon_installed_auto": ("Daemon installed — checks every 60 s and switches automatically.",
                              "감시 데몬 설치 — 60초마다 확인하고 자동으로 전환합니다."),
    "daemon_installed_watch": (
        "Daemon installed in watch-only mode — it notifies you but does not switch. "
        "Turn on switching with `limitpilot config auto-switch on`.",
        "감시 데몬 설치(감시·알림만) — 전환은 하지 않고 알려만 줍니다. "
        "`limitpilot config auto-switch on` 으로 자동 전환을 켤 수 있습니다."),
    "daemon_removed": ("Daemon removed: {name}", "감시 데몬 제거 완료: {name}"),
    # help
    "help_demo": ("practice mode: three fake accounts, nothing real is touched",
                  "연습 모드: 가짜 계정 3개, 실제 자격증명·네트워크를 건드리지 않음"),
    "help_lang": ("output language", "출력 언어"),
    "help_login": ("add another account through a browser login (active account unchanged)",
                   "브라우저 로그인으로 새 계정을 슬롯에 추가(활성 무변경)"),
    "help_alias": ("display name (optional)", "표시용 별칭(생략 가능)"),
    "help_add": ("register the account Claude Code is signed in with",
                 "현재 로그인된 활성 계정을 슬롯에 등록"),
    "help_gateway_add": ("register a third-party gateway from a local env file (manual only)",
                         "로컬 env 의 서드파티 게이트웨이를 수동 전용 슬롯에 등록"),
    "help_env_file": (
        "local KEY=VALUE file with ANTHROPIC_BASE_URL, ANTHROPIC_MODEL and ANTHROPIC_AUTH_TOKEN "
        "(default: gateway.local.env in the current folder)",
        "ANTHROPIC_BASE_URL·ANTHROPIC_MODEL·ANTHROPIC_AUTH_TOKEN 을 담은 로컬 KEY=VALUE 파일 "
        "(기본: 현재 폴더의 gateway.local.env)"),
    "help_remove": ("remove a slot (not the active one)", "슬롯 등록 해제(활성 슬롯 불가)"),
    "help_enable": ("slot ON — candidate for switching", "슬롯 ON — 자동 전환 후보에 포함"),
    "help_disable": ("slot OFF — never switched to automatically",
                     "슬롯 OFF — 자동 전환 후보에서 제외(활성 슬롯 불가)"),
    "help_config": ("show or change settings", "설정 조회/변경"),
    "help_config_key": ("threshold · weekly-threshold · auto-switch · language · keepalive · "
                        "final-burn · priority",
                        "threshold · weekly-threshold · auto-switch · language · keepalive · "
                        "final-burn · priority"),
    "help_config_value": ("new value (omit to show the current one)", "설정값 (생략 시 현재값 조회)"),
    "help_limits": ("per-account session / weekly thresholds and weekly max (default = global)",
                    "계정별 세션 임계·주간 임계·주간 최대치 조회/지정 (default = 전역값)"),
    "help_limits_session": ("session threshold % or default", "세션 임계 % 또는 default"),
    "help_limits_weekly": ("weekly threshold % or default", "주간 임계 % 또는 default"),
    "help_limits_cap": ("weekly max % (100 = none)", "주간 최대치 % (100 = 없음)"),
    "help_cap": ("per-account weekly max (%) — 100 removes it",
                 "계정별 주간 사용 상한(%) 조회/지정 — 100이면 해제"),
    "help_refresh": ("refresh usage now (all slots when omitted)",
                     "사용량 즉시 새로고침(슬롯 생략 시 전체)"),
    "help_keepalive": ("run the keep-alive call now", "유지 호출 즉시 실행"),
    "help_keepalive_force": ("every eligible account, ignoring the schedule",
                             "주기와 무관하게 대상 계정 전체"),
    "help_list": ("show usage and state of every account", "등록 계정의 사용량/상태 표시"),
    "help_status": ("same as list", "list 와 동일"),
    "help_switch": ("switch account (best candidate when the slot is omitted)",
                    "계정 전환(슬롯 생략 시 가장 알맞은 계정으로)"),
    "help_switch_force": ("allow OFF / needs-login / over-weekly-max slots or overwrite an unregistered login",
                          "OFF/격리/주간 최대치 초과 슬롯 선택 또는 현재 미등록 계정 덮어쓰기"),
    "switch_over_cap": ("Slot {n} is over its weekly max ({pct:.0f}% >= {cap:.0f}%) - not switching. "
                        "Raise it with `cap {n} <pct>`, or use `switch {n} --force` "
                        "(auto-switch may move away again on the next check)",
                        "슬롯 {n}은 주간 최대치를 넘었습니다({pct:.0f}% >= {cap:.0f}%) - 전환하지 않습니다. "
                        "`cap {n} <퍼센트>` 로 올리거나 `switch {n} --force` 를 쓰세요"
                        "(자동 전환이 켜져 있으면 다음 확인에서 다시 옮겨질 수 있음)"),
    "switch_over_cap_forced": ("Warning: slot {n} is over its weekly max ({pct:.0f}% >= {cap:.0f}%) - "
                               "switching anyway (--force); auto-switch may move away again on the next check",
                               "경고: 슬롯 {n}은 주간 최대치를 넘었습니다({pct:.0f}% >= {cap:.0f}%) - "
                               "--force 로 전환합니다(자동 전환이 켜져 있으면 다음 확인에서 다시 옮겨질 수 있음)"),
    "help_auto": ("watch usage (and switch when auto-switch is on)",
                  "사용량 감시(자동 전환이 켜져 있으면 전환)"),
    "help_auto_once": ("run one check and exit (used by the daemon)", "1틱 실행 후 종료(데몬용)"),
    "help_auto_dry_run": ("decide only, change nothing", "전환하지 않고 결정만 기록"),
    "help_install_daemon": ("install the 60-second background check", "상시 감시 작업 등록"),
    "help_enable_auto": ("also turn on automatic switching (opt-in)", "자동 전환도 켬(명시적 동의)"),
    "help_uninstall_daemon": ("remove the background check", "상시 감시 작업 해제"),
    "help_purge": ("delete the daemon, account backups, state and logs",
                   "도구 데몬·계정 백업·상태·로그 완전 삭제"),
    "help_purge_yes": ("skip the typed confirmation", "확인 입력 생략"),
    "help_dashboard": ("open the local dashboard (127.0.0.1)", "로컬 대시보드 열기(127.0.0.1)"),
    "help_dashboard_port": ("port (default 8748 or LIMITPILOT_PORT)", "포트(기본 8748 또는 LIMITPILOT_PORT)"),
    "help_dashboard_no_open": ("do not open a browser", "브라우저를 열지 않음"),
    "help_dashboard_app_browser": ("path of a Chromium-based browser for an app window",
                                   "앱 창으로 열 Chromium 계열 브라우저 경로"),
    "help_dashboard_add": ("open with the add-account panel", "계정 추가 패널을 펼친 채 열기"),
    "help_migrate": ("move data from claude-account-pilot", "claude-account-pilot 데이터 옮기기"),
    "help_migrate_dry_run": ("show the steps without changing anything", "아무것도 바꾸지 않고 단계만 표시"),
    "help_migrate_purge": ("after a migration, delete the old folder", "이전이 끝난 뒤 옛 폴더 삭제"),
    "help_doctor": ("check the environment", "환경 점검"),
    "help_widget": ("build / install / uninstall the menu bar (tray) app",
                    "메뉴바(트레이) 앱 빌드·설치·제거"),
    "help_widget_out": ("output folder for build", "build 출력 폴더"),
    "help_widget_dry_run": ("print the steps only", "단계만 출력"),
    "help_tour": ("30-second practice run with fake accounts (implies --demo)",
                  "가짜 계정으로 30초 체험(--demo 포함)"),
    # demo tour
    "tour_title": ("LimitPilot practice tour — practice (mock) mode: 3 fake accounts, virtual clock, "
                   "fake credential store in a temp folder",
                   "LimitPilot 연습 투어 — 연습용 목(mock) 모드: 가짜 계정 3개, 가상 시계, "
                   "임시 폴더의 가짜 자격증명 저장소"),
    "tour_header": ("tick  time   slot 1 5h/7d   slot 2 5h/7d   slot 3 5h/7d   active  action",
                    "틱    시각   슬롯1 5h/7d    슬롯2 5h/7d    슬롯3 5h/7d    활성    동작"),
    "tour_no_action": ("no action", "조치 없음"),
    "tour_switch": ("switch slot {a} -> slot {b} (reason: {reason}; slot {b} weekly reset in {hours} h)",
                    "switch slot {a} -> slot {b} (사유: {reason}; 슬롯 {b} 주간 리셋 {hours}시간 뒤)"),
    "tour_excluded": ("slot {n} excluded: needs login (token refresh failed: {why})",
                      "slot {n} excluded: needs login (토큰 갱신 실패: {why})"),
    "tour_active_now": ("fake active credential now holds: {token} (slot {n})",
                        "가짜 활성 자격증명 현재 값: {token} (슬롯 {n})"),
    "tour_real_code": ("Real code paths used: policy.decide.decide, switch.perform_switch "
                       "(read-back + rollback), state.save_state.",
                       "실제로 돈 코드: policy.decide.decide, switch.perform_switch(read-back·롤백), "
                       "state.save_state."),
    # 입력 검증 오류
    "lbl_threshold": ("Threshold", "임계치"),
    "lbl_session_threshold": ("Session threshold", "세션 임계"),
    "lbl_weekly_threshold": ("Weekly threshold", "주간 임계"),
    "lbl_weekly_cap": ("Weekly cap", "주간 상한"),
    "must_be_number": ("{what} must be a number (got {value!r})",
                       "{what}은(는) 숫자여야 합니다 (입력: {value!r})"),
    "must_be_in_range": ("{what} must be between {lo}% and {hi}%",
                         "{what}은(는) {lo}~{hi}% 범위여야 합니다"),
    "must_be_on_off": ("Use on or off", "on 또는 off로 지정하세요"),
    "must_be_one_of": ("{what} must be one of: {choices}",
                       "{what}은(는) 다음 중 하나여야 합니다: {choices}"),
    "lbl_language": ("Language", "언어"),
    "lbl_priority": ("Priority", "우선순위"),
    "unknown_setting": ("Unknown setting key: {key}", "알 수 없는 설정 키: {key}"),
    "cap_subscription_only": ("A weekly cap can only be set on subscription accounts",
                              "주간 상한은 구독 계정에만 지정할 수 있습니다"),
    "limits_subscription_only": ("Thresholds and caps can only be set on subscription accounts",
                                 "임계·최대치는 구독 계정에만 지정할 수 있습니다"),
    "slot_cap_set": ("Slot {n} ({email}) weekly cap {v}%", "슬롯 {n}({email}) 주간 상한 {v}%"),
    "slot_limits_set": ("Slot {n} ({email}) limits changed: session {s}% · weekly {w}% · max {c}%",
                        "슬롯 {n}({email}) 임계 변경: 세션 {s}% · 주간 {w}% · 최대 {c}%"),
    "already_active_account": ("This account is already registered as the active slot",
                               "이미 활성 슬롯에 등록된 계정입니다"),
    "cannot_remove_active": ("The active account cannot be removed — switch to another one first",
                             "활성 계정은 삭제할 수 없습니다 — 먼저 다른 계정으로 전환하세요"),
    "cannot_disable_active": ("The active account cannot be turned off — switch to another one first",
                              "활성 계정은 끌 수 없습니다 — 먼저 다른 계정으로 전환하세요"),
    "cannot_enable_setup_token": (
        "Slot {n} is a limited token and cannot be turned on — remove it and add it again in the browser",
        "슬롯 {n}은 제한 토큰이라 켤 수 없습니다 — 삭제 후 브라우저로 다시 추가하세요"),
    "alias_not_string": ("The alias must be a string", "별칭은 문자열이어야 합니다"),
    "alias_too_long": ("The alias must be 80 characters or fewer", "별칭은 80자 이하여야 합니다"),
    "slot_removed": ("Slot {n} removed: {email}", "슬롯 {n} 제거: {email}"),
    "slot_toggled": ("Slot {n} ({email}) {state}", "슬롯 {n}({email}) {state}"),
    "backup_file_delete_failed": ("Could not delete the backup file of slot {n} - the slot is kept",
                                  "슬롯 {n} 백업 파일 삭제 실패 — 슬롯은 유지됩니다"),
    "keychain_delete_failed": ("Could not delete the keychain entry of slot {n} - the slot is kept",
                               "슬롯 {n} 키체인 삭제 실패 — 슬롯은 유지됩니다"),
    "keychain_delete_failed_rc": (
        "Could not delete the keychain entry of slot {n} (rc={rc}) - the slot is kept",
        "슬롯 {n} 키체인 삭제 실패(rc={rc}) — 슬롯은 유지됩니다"),
    "limit_partial_usage": ("partial usage", "사용량 일부"),
    "limit_session": ("session", "세션"),
    "limit_weekly": ("weekly", "주간"),
    "port_out_of_range": ("port must be a whole number from 1 to 65535 (got {value!r})",
                          "포트는 1~65535 사이의 정수여야 합니다 (입력: {value!r})"),
    # 연습 모드 데몬 · 감시 중지 안내
    "practice_daemon_install": (
        "Practice mode: would register the background watcher with the OS scheduler "
        "(nothing was changed){auto}",
        "연습 모드: 백그라운드 감시를 OS 스케줄러에 등록하는 단계입니다 (실제로는 바뀐 것 없음){auto}"),
    "practice_daemon_auto": (" - auto-switch turned on in the practice state",
                             " — 연습 상태에서만 자동 전환을 켰습니다"),
    "practice_daemon_uninstall": (
        "Practice mode: would remove the background watcher from the OS scheduler "
        "(nothing was changed)",
        "연습 모드: 백그라운드 감시를 OS 스케줄러에서 제거하는 단계입니다 (실제로는 바뀐 것 없음)"),
    # 연습 모드 로그인 · 유지 호출 — 진짜 claude 실행·메시지 전송 대신 무엇을 할지만 알린다
    "practice_login": (
        "Practice mode: would run `claude auth login` in an isolated folder and save the new "
        "account to a backup slot (nothing was changed). Run without --demo to add a real account.",
        "연습 모드: 격리된 폴더에서 `claude auth login` 을 실행하고 새 계정을 백업 슬롯에 저장하는 "
        "단계입니다 (실제로는 바뀐 것 없음). 실제 계정은 --demo 없이 추가하세요."),
    "practice_login_dashboard": (
        "Practice mode: browser sign-in would run `claude auth login` in an isolated folder, "
        "so it is off with fake accounts (nothing was changed)",
        "연습 모드: 브라우저 로그인은 격리된 폴더에서 `claude auth login` 을 실행하므로 "
        "가짜 계정에서는 꺼져 있습니다 (실제로는 바뀐 것 없음)"),
    "practice_keepalive": (
        "Practice mode: would send one minimal request per account (max_tokens=1) "
        "for slot(s) {slots} (nothing was sent)",
        "연습 모드: 계정마다 최소 요청(max_tokens=1) 1건을 보내는 단계입니다 — 대상 슬롯 {slots} "
        "(실제로는 보낸 것 없음)"),
    "sandbox_blocked": (
        "Blocked in practice mode: {detail}. Run without --demo to use the real system.",
        "연습 모드에서 차단됨: {detail}. 실제 시스템을 쓰려면 --demo 없이 실행하세요."),
    "watcher_stopped": (
        "The watcher is stopped (uninstall-daemon was run). "
        "Run `limitpilot install-daemon` to resume.",
        "감시가 중지된 상태입니다(uninstall-daemon 실행됨). "
        "`limitpilot install-daemon` 으로 다시 시작하세요."),
    "watcher_purging": ("A full purge is in progress, so nothing is watched.",
                        "완전 삭제가 진행 중이라 감시하지 않습니다."),
    "unexpected_error": ("{err} - run `limitpilot doctor` to check your setup "
                         "(set LIMITPILOT_DEBUG=1 for the traceback)",
                         "{err} — `limitpilot doctor` 로 점검하세요 (자세한 내용은 LIMITPILOT_DEBUG=1)"),
    # 대시보드 시작 메시지
    "dash_already_running": ("Already running: {url}", "이미 실행 중: {url}"),
    "dash_focus_existing": ("Already open - bringing the existing window forward: {url}",
                            "이미 열려 있음 — 기존 창을 앞으로: {url}"),
    "dash_reopen": ("Already running - opening a new window: {url}",
                    "이미 실행 중 — 새 창을 엽니다: {url}"),
    "dash_purging": ("A full purge is in progress, so the dashboard will not start",
                     "완전 삭제가 진행 중이라 대시보드를 시작하지 않습니다"),
    "dash_port_busy": ("Port {port} is used by another program (try --port)",
                       "포트 {port}을 다른 프로그램이 사용 중입니다 (--port 로 다른 포트 지정)"),
    "dash_newer_running": ("A newer dashboard ({version}) is already running - using it",
                           "더 새 버전({version}) 대시보드가 실행 중이라 그대로 사용합니다"),
    "dash_add_in_progress": ("An account is being added, so the existing dashboard is used this time",
                             "계정 추가가 진행 중이라 이번에는 기존 대시보드를 사용합니다"),
    "dash_old_not_stopped": (
        "Could not stop the older dashboard, so it is used as is - close its window and process, then open again",
        "이전 버전 대시보드를 종료하지 못해 그대로 사용합니다 — 대시보드 창과 프로세스를 닫고 다시 여세요"),
    "dash_replacing": ("Stopping the older dashboard and starting a new one (build {build})",
                       "이전 버전 대시보드를 종료하고 새로 시작합니다 (build {build})"),
    "dash_cannot_start": ("Cannot start the dashboard: {err}", "대시보드를 시작할 수 없습니다: {err}"),
    "dash_runtime_save_failed": ("Could not save the dashboard run record: {err}",
                                 "대시보드 실행 기록을 저장할 수 없습니다: {err}"),
    "dash_started": ("Dashboard started: {url}", "대시보드 시작: {url}"),
    "dash_demo_suffix": (" (DEMO - practice mock mode)", " (DEMO — 연습용 목 모드)"),
    "dash_open_practice": (
        "Practice mode: open {url} in your browser yourself (practice mode starts no other programs)",
        "연습 모드: 브라우저에서 {url} 을 직접 여세요 (연습 모드는 다른 프로그램을 실행하지 않습니다)"),
    "dash_open_failed": ("Could not open a browser ({err}) - open {url} yourself",
                         "브라우저를 열지 못했습니다({err}) — {url} 을 직접 여세요"),
}

from limitpilot.i18n_more import MORE as _MORE  # noqa: E402

_PAIRS.update(_MORE)

MESSAGES = {
    "en": {k: v[0] for k, v in _PAIRS.items()},
    "ko": {k: v[1] for k, v in _PAIRS.items()},
}


def _from_locale():
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            return "ko" if value.lower().startswith("ko") else "en"
    try:
        loc = locale.getlocale()[0] or ""
    except (ValueError, TypeError):
        loc = ""
    return "ko" if loc.lower().startswith("ko") else "en"


def default_lang() -> str:
    """Language when nothing was chosen explicitly: env override, then OS locale, else en."""
    env = (os.environ.get("LIMITPILOT_LANG") or "").lower()
    if env in LANGS:
        return env
    return _from_locale()


def set_lang(lang) -> None:
    global _override
    if lang in LANGS:
        _override = lang
        os.environ["LIMITPILOT_LANG"] = lang  # 하위 프로세스(대시보드 등)도 같은 언어


def current_lang() -> str:
    return _override or default_lang()


def t(key: str, /, **kw) -> str:
    table = MESSAGES[current_lang()]
    text = table.get(key) or MESSAGES["en"].get(key) or key
    return text.format(**kw) if kw else text


def tl(lang: str, key: str, /, **kw) -> str:
    """Like ``t`` but for an explicit language (the dashboard serves ``settings.language``)."""
    text = MESSAGES.get(lang, MESSAGES["en"]).get(key) or MESSAGES["en"].get(key) or key
    return text.format(**kw) if kw else text
