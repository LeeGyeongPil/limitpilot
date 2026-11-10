"""Switching the active account: write the backup credential into Claude Code's store,
splice the matching `oauthAccount` into `~/.claude.json`, read both back, and roll back
on any mismatch. Both files are written under Claude Code's own lock directories.
"""

import os
import time

from limitpilot.errors import (KeychainError, SwitchError)
from limitpilot import accounts
from limitpilot import gateway
from limitpilot import i18n
from limitpilot import locks
from limitpilot import logutil
from limitpilot import notify as notify_mod
from limitpilot import paths
from limitpilot.policy import limits
from limitpilot import state
from limitpilot.store import creds
from limitpilot.store import filestore


# --------------------------------------------------------------------- swap


def splice_oauth_account(target_oauth: dict) -> dict:
    """~/.claude.json의 oauthAccount만 교체, 나머지 키 전부 라운드트립 보존.
    반환값 = 교체 전 oauthAccount(롤백용)."""
    cfg = filestore.read_json(paths.CLAUDE_JSON)
    prev = cfg.get("oauthAccount")
    cfg["oauthAccount"] = target_oauth
    filestore.atomic_write_json(paths.CLAUDE_JSON, cfg)
    return prev


def bump_creds_mtime() -> None:
    # Claude Code는 이 파일 mtime 변경 시 메모리 토큰 캐시를 무효화 — 없으면 생성 금지
    if os.path.exists(paths.CC_CREDS_FILE):
        try:
            os.utime(paths.CC_CREDS_FILE, None)
        except OSError:
            pass


def rollback_active(live) -> None:
    """전환 실패 시 활성 키체인을 이전 자격증명으로 원복하고 read-back으로 재검증.
    원복 실패는 원래 오류를 가리지 않도록 전파하지 않되, 키체인/~/.claude.json
    불일치 가능성을 크게 알린다(방치 시 다음 전환의 되백업이 백업을 오염시킴)."""
    if live is None:
        return
    try:
        creds.kc_write_active(live)
        if creds.kc_read_active() != live:
            raise KeychainError(i18n.t("sw_rollback_mismatch"))
    except Exception as e:
        logutil.log(i18n.t("sw_rollback_fatal", err=e))
        notify_mod.notify(i18n.t("sw_rollback_title"), i18n.t("sw_rollback_body"))


def perform_switch(
    st: dict, target: int, dry_run: bool = False, force: bool = False
) -> str:
    """활성 계정을 target 슬롯으로 전환. 반환: "switched"|"already".
    호출자가 state_lock을 보유해야 하며, 성공 시 st가 갱신·저장된다."""
    tgt = state.slot_of(st, target)
    if not tgt:
        raise SwitchError(i18n.t("sw_not_registered", n=target))
    if tgt.get("kind") == "setup-token":
        raise SwitchError(i18n.t("sw_restricted", n=target))
    if not tgt.get("enabled", True) and not force and not limits.is_manual_only_slot(tgt):
        # 엔터프라이즈는 enabled(자동전환 포함) 플래그와 무관하게 수동 전환 대상 —
        # OFF여도 사용자가 명시적으로 고르면 전환한다(정책상 수동 전용).
        raise SwitchError(i18n.t("sw_off", n=target))
    if tgt.get("quarantine") and not force:
        raise SwitchError(i18n.t("sw_quarantined", n=target))
    if limits.is_gateway_slot(tgt):
        if dry_run:
            logutil.log(i18n.t("sw_dry_gateway", n=target, label=state.slot_label(tgt, target)))
            return "switched"
        if gateway.gateway_config_is_active(st, target):
            st["activeSlot"] = target
            state.save_state(st)
            return "already"
        gateway.activate_gateway_settings(st, target)
        return "switched"
    tgt_oauth = tgt.get("oauthAccount")
    if not tgt_oauth:
        raise SwitchError(i18n.t("sw_no_oauth", n=target))

    if dry_run:
        logutil.log(i18n.t("sw_dry", n=target, email=tgt.get("email")))
        return "switched"

    with locks.proper_lockfile(paths.CRED_LOCK_DIR):
        live = creds.kc_read_active()
        if live == "":
            # 빈 문자열 = 키체인 잠금/타임아웃 아티팩트 가능성 — 좋은 백업을 덮어쓰지 않음
            raise SwitchError(i18n.t("sw_empty_live"))

        live_oauth = accounts.read_live_oauth_account()
        live_email = (live_oauth or {}).get("emailAddress")
        cur = (
            accounts.find_slot_by_uuid(st, live_oauth)
            if accounts.identity_uuid_pair(live_oauth)
            else accounts.find_slot_by_identity(st, live_oauth)
        )

        # 등록되지 않은 수동 로그인은 어느 슬롯에도 안전하게 되백업할 수 없다.
        # 자동/일반 전환에서는 덮어쓰지 않고, 사용자가 명시한 force에서만 진행한다.
        if live is not None and cur is None and not force:
            if accounts.identity_uuid_pair(live_oauth):
                raise SwitchError(i18n.t("sw_outside"))
            raise SwitchError(i18n.t("sw_unidentified"))

        # 스왑 전 필수: 현재 라이브 자격증명을 자기 슬롯에 되백업(Claude Code의
        # 토큰 리프레시로 회전된 최신 토큰을 잃지 않기 위함)
        if live is not None and cur is not None:
            creds.kc_write_slot(cur, live)
            if live_oauth:
                st["slots"][str(cur)]["oauthAccount"] = state.extract_identity(live_oauth)
                st["slots"][str(cur)]["email"] = live_email
        elif live is not None and cur is None:
            logutil.log(i18n.t("sw_warn_unregistered", email=live_email or i18n.t("sw_unknown")))

        if cur == target and live is not None:
            if st.get("gatewayRestore") is not None:
                gateway.deactivate_gateway_settings(st)
            st["activeSlot"] = target
            state.save_state(st)
            return "already"

        tgt_blob = creds.kc_read_slot(target)
        if not tgt_blob:
            raise SwitchError(i18n.t("sw_no_backup", n=target))

        creds.kc_write_active(tgt_blob)
        try:
            back = creds.kc_read_active()
        except KeychainError as e:
            # read-back 자체가 실패하면 검증 불가 — 활성이 target으로 바뀐 채
            # 방치하면 다음 전환의 되백업이 이전 슬롯을 오염시키므로 즉시 원복
            rollback_active(live)
            raise SwitchError(i18n.t("sw_verify_unavailable", err=e)) from e
        if back != tgt_blob:
            # verify 실패 — 즉시 원복 시도
            rollback_active(live)
            raise SwitchError(i18n.t("sw_verify_failed"))

        try:
            with locks.proper_lockfile(paths.CONF_LOCK_DIR):
                splice_oauth_account(state.extract_identity(tgt_oauth))
        except Exception:
            rollback_active(live)  # oauthAccount 스플라이스 실패 → 활성 원복
            raise

        bump_creds_mtime()

    # OAuth 자격증명을 활성화한 뒤에만 게이트웨이 override를 제거한다. 앞 단계가
    # 실패하면 사용자가 선택한 게이트웨이는 그대로 유지된다.
    if st.get("gatewayRestore") is not None:
        gateway.deactivate_gateway_settings(st)
    st["activeSlot"] = target
    st["lastSwitchAt"] = time.time()
    st["lastSwitchTo"] = target
    state.save_state(st)
    return "switched"
