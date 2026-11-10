"""Tuning constants shared by the decision, collection and daemon code."""

import re

from limitpilot import __version__


LOCK_STALENESS_S = 10.0
LOCK_TOUCH_S = 3.0
LOCK_TIMEOUT_S = 9.0
STATE_SCHEMA = 1
PURGE_KEYCHAIN_ITEM_LIMIT = 10_000

# security CLI
KC_NOT_FOUND_RC = 44
LAUNCHCTL_NOT_FOUND_RC = 113
KC_TIMEOUT_S = 5.0
KC_READ_ATTEMPTS = 2
KC_READ_RETRY_S = 0.3
KC_WRITE_ATTEMPTS = 4  # 유일 사본(회전 토큰) 쓰기용 — 일시 잠금에 토큰 유실 방지
KC_WRITE_RETRY_S = 0.5
KC_STDIN_LIMIT = 4096 - 64  # security -i의 줄 버퍼(fgets 4096) 초과 시 조용한 절단 방지

# OAuth / Usage
APP_VERSION = __version__  # 버전의 유일한 출처는 limitpilot/__init__.py
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# Claude Code 의 공개 OAuth client id — 비밀 값이 아니다(모든 Claude Code 설치에 같은 값).
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # gitleaks:allow
BETA_HEADER = "oauth-2025-04-20"
USER_AGENT = f"limitpilot/{APP_VERSION}"
EXPIRY_BUFFER_MS = 5 * 60 * 1000
FRESHEN_BUFFER_MS = 10 * 60 * 1000

# 브라우저 로그인 추가 — 별도 CLAUDE_CONFIG_DIR에서 공식 `claude auth login` 실행
LOGIN_TIMEOUT_S = 300
LOGIN_REQUIRED_SCOPES = frozenset({
    "user:profile", "user:inference", "user:sessions:claude_code",
    "user:mcp_servers", "user:file_upload",
})
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# 결정 파라미터 (THRESHOLD는 설정으로 조절 가능 — 아래 get_settings 참조)
THRESHOLD_PCT = 90.0       # 기본 자동전환 임계
THRESHOLD_MIN = 50.0
THRESHOLD_MAX = 99.0
LANGUAGES = frozenset({"ko", "en"})  # 위젯/트레이/대시보드 UI 언어
DEFAULT_LANGUAGE = "ko"
HYSTERESIS_PCT = 10.0      # 후보 적격 기준선 = THRESHOLD - HYSTERESIS
COOLDOWN_S = 300.0
UNHEALTHY_TICKS = 3
INTERVAL_S = 60.0
SESSION_WINDOW_S = 5 * 3600.0  # 구독 5시간 세션 창 — 미시작 창의 리셋 예상치 계산용
RESET_TIE_MAX_PCT = 20.0  # 세션 리셋 이점이 상쇄할 수 있는 사용률 차이의 상한(%p).
                          # 후보의 5시간 창 리셋이 임박할수록 이만큼까지 사용률을
                          # 깎아 준다(candidate_score 참조)
# 계정별 한도 — 창마다 '임계(여기서 선제 전환)'와 '최대치(여기서 즉시 전환·후보 제외)'.
# 세션 임계·주간 임계는 전역값(settings.threshold / settings.weeklyThreshold)을 계정별로
# 덮어쓸 수 있고(sessionThreshold / weeklyThreshold), 주간 최대치는 계정별(weeklyCap,
# 기본 100). 결정 로직은 각 창을 '임계→전역 세션 임계, 최대치→100'으로 정규화한 값만
# 본다(slot_decision_usage) — 기존 임계·히스테리시스·즉시 전환 규칙이 그대로 통한다.
WEEKLY_CAP_MIN = 10.0
WEEKLY_CAP_MAX = 100.0
# 후보 우선순위 — expiring: 주간 리셋이 임박한(쓰지 않으면 사라질) 여유분부터 사용,
# balanced: 종전 동작(사용률 최저 + 세션 리셋 타이브레이크)
PRIORITIES = frozenset({"expiring", "balanced"})
DEFAULT_PRIORITY = "expiring"
EXPIRING_BUCKET_S = 12 * 3600.0       # 주간 리셋 시각 비교 해상도 — 이 안의 차이는 동급
EXPIRING_MIN_HEADROOM_PCT = 10.0      # 리셋 임박 우선을 받을 최소 주간 여유(상한 환산 %p)
EXPIRING_SOON_S = 24 * 3600.0         # 이 안에 리셋되는 여유분은 임계 미만이어도 선제 이동
REBALANCE_COOLDOWN_S = 1800.0         # 리셋 임박 선제 이동 쿨다운(일반 전환 쿨다운보다 길게)
# 리셋 직전 소진(settings.finalBurn, 기본 켬) — 주간 리셋이 이 안으로 다가오면 그 계정의
# 주간 임계와 최대치를 모두 100%로 올려 남은 한도를 다 쓴다(리셋되면 어차피 사라진다).
FINAL_BURN_S = 5 * 3600.0             # 주간 리셋 이 안: 그 계정의 모든 임계·최대치 무시, 100%까지
SESSION_BURN_S = 30 * 60.0            # 세션 리셋 이 안: 세션 임계 무시, 세션 100%까지(주간 한도는 지킴)
FINAL_MIN_ROOM_PCT = 5.0              # 리셋 직전 계정으로 옮길 만한 최소 여유(%p)
BURN_COOLDOWN_S = 300.0               # 리셋 직전 소진 이동 쿨다운 — 창이 짧아 일반 선제 이동보다 짧게
BURN_MANUAL_HOLD_S = 1800.0           # 직접 고른 계정을 리셋 직전 소진 이동이 덮지 않는 시간
BURN_RANK_BASE = -1e9                 # 소진 대상 후보 순위(리셋 임박 버킷보다 항상 앞)
REBALANCE_MIN_ROOM_PCT = 5.0          # 선제 이동 대상의 주간(환산)이 임계보다 이만큼은 낮아야 함
REBALANCE_MIN_SESSION_ROOM_PCT = 20.0 # 선제 이동 대상의 세션이 임계보다 이만큼은 낮아야 함 —
                                      # 옮기자마자 세션 임계로 되돌아오는 왕복 전환 방지
MANUAL_HOLD_S = 5 * 3600.0            # 사용자가 직접 고른 계정은 이 시간 동안 선제 이동 안 함
# 일일 유지 호출(keepalive) — 계정마다 하루 1회 최소(max_tokens=1) 메시지를 보내
# 자격증명과 사용 창을 살려 둔다. 데몬 1틱에 최대 1계정만 처리한다.
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
KEEPALIVE_MODEL = "claude-haiku-4-5-20251001"
KEEPALIVE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
KEEPALIVE_INTERVAL_S = 24 * 3600.0
KEEPALIVE_RETRY_S = 3600.0            # 일시 실패(네트워크·5xx·429) 재시도 간격
KEEPALIVE_TIMEOUT_S = 15.0
MANUAL_REFRESH_MIN_S = 30.0           # 수동 새로고침 연타 방지 — 이 안의 재조회는 생략
# usage API 는 너무 자주 부르면 429 를 돌려준다. 기본 조회 주기를 넉넉히 잡고, 429 를
# 받으면 지수 백오프로 물러난다. 전환 판단이 급한 순간에만 매 틱 조회로 올린다.
FETCH_BACKOFF_BASE_S = 120.0  # 429 백오프: 2→4→8→15분(캡). 성공 시 리셋
FETCH_BACKOFF_CAP_S = 900.0
ACTIVE_PROBE_S = 180.0        # 활성 슬롯 정기 실측 주기(임계 근접/불명 시 매 틱 승격)
CANDIDATE_PROBE_S = 600.0     # 비활성 슬롯(OFF 포함 — 표시용) 정기 실측 주기
PROBE_MARGIN_PCT = 15.0       # 활성이 (임계-마진)% 이상/측정 불가면 활성·후보 매 틱 실측
RESET_SLACK_S = 60.0
MAX_SLEEP_S = 6 * 3600.0
STALE_OK_S = 420.0  # lastGood usage가 이보다 오래면 결정에 사용하지 않음
                    # (ACTIVE_PROBE_S 180s + 429 백오프 120s + 여유 — 정상 실측 사이에
                    #  한 번 실패해도 stale로 굳지 않을 만큼)
NOTIFY_DEDUPE_S = 3600.0
TOKEN_EXPIRED = "token-expired"  # 센티넬
UNMANAGED = "unmanaged"  # 센티넬 — 활성 키체인이 등록 슬롯과 다른 계정(수동 로그인)

# 엔터프라이즈(usage-based) 계정 — 세션/주간 % 한도 대신 월 달러 소진 한도를 쓴다.
# usage 응답에 five_hour/seven_day/limits[]가 비고 spend(달러 한도)만 온다. 자동전환
# 후보/failover에서 제외하고 오직 수동 전환으로만 쓴다(아래 is_enterprise_slot 참조).
ENTERPRISE_ORG_TYPE = "claude_enterprise"
GATEWAY_KIND = "gateway"
GATEWAY_ENV_KEYS = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
    "ANTHROPIC_CUSTOM_MODEL_OPTION", "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION", "MAX_THINKING_TOKENS",
)

# --once 종료코드
EXIT_SWITCHED, EXIT_ERROR, EXIT_NO_ACTION, EXIT_BLOCKED, EXIT_PURGING = 0, 1, 2, 3, 4


def reason_all_exhausted() -> str:
    """Blocked-tick reason text in the current language (compared by identity of text)."""
    from limitpilot import i18n
    return i18n.t("reason_all_exhausted")
