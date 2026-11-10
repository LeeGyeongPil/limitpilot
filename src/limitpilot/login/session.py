"""Add an account through the official browser login, without touching the active account.

``claude auth login`` runs with its own ``CLAUDE_CONFIG_DIR`` inside a pseudo-terminal
(POSIX pty, Windows ConPTY), so Claude Code's active credential is never replaced; the
new credential is copied into a backup slot and the temporary profile is removed.
"""

LOGIN_BUFFER_MAX = 64 * 1024   # 로그인 터미널 출력 보관 상한(문자)

import hashlib
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from datetime import datetime, timezone

if sys.platform == "win32":
    pass
else:
    import fcntl
    import pty
    import select
    import termios

from limitpilot.constants import (LOGIN_REQUIRED_SCOPES, LOGIN_TIMEOUT_S, _EMAIL_RE)
from limitpilot.errors import (LimitPilotError, KeychainError)
from limitpilot.login import conpty_win
from limitpilot import i18n
from limitpilot import logutil
from limitpilot import oauth as oauth_mod
from limitpilot import paths
from limitpilot import state
from limitpilot.store import filestore
from limitpilot.store import keychain


# ----------------------------------------------------------------- commands


def find_claude_bin():
    found = shutil.which("claude")
    if found and paths.IS_WIN:
        # Windows의 which()는 CWD를 PATH 앞에 끼워넣는다(3.11 이하 항상) — 작업
        # 디렉터리에 심어둔 가짜 claude가 격리 로그인의 수확 대상이 되는 것을 차단
        try:
            if os.path.dirname(os.path.abspath(found)) == os.getcwd():
                found = None
        except OSError:
            found = None
    return found or next(
        (p for p in paths._CLAUDE_BIN_CANDIDATES if os.path.exists(p)), None)


def _strip_ansi(s: str) -> str:
    s = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", s)
    s = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", s)
    return s.replace("\r", "")


def isolated_keychain_service(config_dir: str) -> str:
    """Claude Code가 CLAUDE_CONFIG_DIR별로 사용하는 macOS Keychain 서비스명."""
    raw = unicodedata.normalize("NFC", config_dir)
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{paths.CLAUDE_KEYCHAIN_BASE}-{suffix}"


def _isolated_config_paths(config_dir: str):
    # Claude Code 버전에 따라 둘 중 하나를 사용한다. credentials 파일은
    # Keychain 접근이 불가능한 환경에서만 쓰는 공식 폴백이다.
    return (
        os.path.join(config_dir, ".config.json"),
        os.path.join(config_dir, ".claude.json"),
    )


def validate_login_credentials(blob: str, oauth_account: dict):
    """격리 로그인 결과가 전환 가능한 정식 Claude OAuth 계정인지 검증한다."""
    oauth = oauth_mod.parse_oauth(blob)
    if not oauth:
        raise LimitPilotError(i18n.t("lg_bad_format"))
    if not isinstance(oauth.get("accessToken"), str) or not oauth["accessToken"]:
        raise LimitPilotError(i18n.t("lg_no_access"))
    if not isinstance(oauth.get("refreshToken"), str) or not oauth["refreshToken"]:
        raise LimitPilotError(i18n.t("lg_limited_token"))
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise LimitPilotError(i18n.t("lg_no_expiry"))
    scopes = oauth.get("scopes")
    if not isinstance(scopes, list) or not all(isinstance(v, str) for v in scopes):
        raise LimitPilotError(i18n.t("lg_no_scopes"))
    missing = LOGIN_REQUIRED_SCOPES.difference(scopes)
    if missing:
        raise LimitPilotError(i18n.t("lg_scopes_missing"))
    if not isinstance(oauth_account, dict):
        raise LimitPilotError(i18n.t("lg_no_profile"))
    email = oauth_account.get("emailAddress")
    account_uuid = oauth_account.get("accountUuid")
    organization_uuid = oauth_account.get("organizationUuid")
    if not isinstance(email, str) or not _EMAIL_RE.fullmatch(email):
        raise LimitPilotError(i18n.t("lg_no_email"))
    if not isinstance(account_uuid, str) or not account_uuid:
        raise LimitPilotError(i18n.t("lg_no_account_id"))
    if not isinstance(organization_uuid, str) or not organization_uuid:
        raise LimitPilotError(i18n.t("lg_no_org_id"))
    return oauth


def load_isolated_login(config_dir: str):
    """격리 프로필의 정식 credentials와 oauthAccount를 메모리로 복사한다."""
    blob = None
    last_error = None
    if not paths.IS_WIN:
        service = isolated_keychain_service(config_dir)
        for attempt in range(5):
            try:
                blob = keychain.kc_read(service, paths.KC_USER)
            except KeychainError as e:
                last_error = e
            if blob:
                break
            if attempt < 4:
                time.sleep(0.2)
    if not blob:
        # Windows 의 Claude Code 는 자격증명을 이 파일에 저장한다(관찰된 동작).
        # macOS 에서는 키체인을 쓸 수 없는 환경에서 Claude Code 가 쓰는 대체 위치다.
        credentials_path = os.path.join(config_dir, ".credentials.json")
        attempts = 5 if paths.IS_WIN else 1
        for attempt in range(attempts):
            try:
                with open(credentials_path, encoding="utf-8") as f:
                    candidate = json.load(f)
                blob = json.dumps(candidate, ensure_ascii=False)
            except (OSError, json.JSONDecodeError, TypeError):
                blob = None  # 로그인 직후 쓰기 지연/AV 잠금 — 잠시 뒤 재시도
            if blob:
                break
            if attempt < attempts - 1:
                time.sleep(0.2)
    if not blob:
        if last_error:
            raise LimitPilotError(i18n.t("lg_read_isolated")) from last_error
        raise LimitPilotError(i18n.t("lg_not_stored"))

    oauth_account = None
    for path in _isolated_config_paths(config_dir):
        try:
            candidate = filestore.read_json(path).get("oauthAccount")
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if isinstance(candidate, dict) and candidate.get("emailAddress"):
            oauth_account = candidate
            break
    validate_login_credentials(blob, oauth_account)
    return blob, state.extract_identity(oauth_account)


def cleanup_isolated_auth(config_dir: str) -> None:
    """임시 로그인만 직접 제거한다. `claude auth logout`은 refresh token을
    revoke하므로 절대 호출하지 않는다."""
    keychain_clean = True
    if not paths.IS_WIN:  # Windows는 Keychain이 없고 자격증명이 profile 파일 안에 있다
        service = isolated_keychain_service(config_dir)
        if not re.fullmatch(r"Claude Code-credentials-[0-9a-f]{8}", service):
            raise LimitPilotError(i18n.t("purge_tmp_service"))
        try:
            keychain.kc_delete(service, paths.KC_USER)
        except KeychainError:
            keychain_clean = False
            # 서비스명은 경로 해시뿐이고 비밀이 아니다. 실패를 숨기지 않아 사용자가
            # `security delete-generic-password -s <service>`로 잔존 항목을 정리할 수 있다.
            logutil.log(i18n.t("lg_kc_cleanup_failed", service=service))

    state_root = os.path.realpath(paths.STATE_DIR)
    profile = os.path.realpath(config_dir)
    safe_parent = os.path.dirname(profile) == state_root
    safe_name = os.path.basename(profile).startswith("oauth-login-")
    # Keychain 삭제가 실패하면 service hash를 다시 계산할 프로필 경로를
    # 남겨 다음 cleanup/purge에서 재시도한다.
    if keychain_clean and safe_parent and safe_name:
        shutil.rmtree(profile, ignore_errors=True)
        if paths.IS_WIN and os.path.exists(profile):
            # Windows profile 파일이 곧 자격증명 — 잠금 등으로 남으면 알려서 수동 정리 유도
            logutil.log(i18n.t("lg_profile_cleanup_failed", path=profile))


def _dump_add_debug(buf: str, exit_code=None) -> None:
    """민감한 PTY 원문 없이 정식 로그인 진행 단계만 기록한다."""
    try:
        flat = _strip_ansi(buf)
        compact = re.sub(r"\s+", "", flat).lower()
        diagnostic = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "outputChars": len(buf),
            "authorizeUrlSeen": "oauth/authorize" in flat,
            "codePromptSeen": "pastecodehereifprompted" in compact,
            "loginSuccessfulSeen": "loginsuccessful" in compact,
            "processReturnCode": exit_code,
        }
        os.makedirs(paths.STATE_DIR, mode=0o700, exist_ok=True)
        path = os.path.join(paths.STATE_DIR, "add-debug.log")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        if hasattr(os, "fchmod"):  # Windows 부재
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(diagnostic, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError:
        pass


class ClaudeLoginSession:
    """공식 `claude auth login --claudeai`를 격리 프로필의 가상 터미널에서 구동한다.

    유닉스는 pty+select, Windows는 ConPTY(표준 라이브러리 ctypes) — 동작 동일.
    CLAUDE_CONFIG_DIR별 저장소(macOS Keychain / Windows credentials 파일)를 쓰므로
    현재 활성 credentials는 바뀌지 않는다. 성공 자격증명을 메모리로 복사한 뒤
    임시 저장소/profile만 직접 제거한다.
    """

    def _append(self, text: str) -> None:
        """Keep only the last LOGIN_BUFFER_MAX characters of terminal output (ring buffer).

        The login can run for minutes; we only ever need the recent output (the URL /
        prompt), so the buffer must not grow without bound.
        """
        self.buf = (self.buf + text)[-LOGIN_BUFFER_MAX:]

    def __init__(self):
        self.proc = None
        self.pid = None
        self.master = None
        self.win = None    # Windows ConPTY 백엔드 (_WinConPty)
        self.buf = ""
        self._io_lock = threading.RLock()
        os.makedirs(paths.STATE_DIR, mode=0o700, exist_ok=True)
        self.config_dir = tempfile.mkdtemp(prefix="oauth-login-", dir=paths.STATE_DIR)
        os.chmod(self.config_dir, 0o700)
        self._cleaned = False

    def _drain(self, seconds: float) -> None:
        if paths.IS_WIN:
            end = time.time() + seconds
            while time.time() < end:
                with self._io_lock:
                    win = self.win
                    if win is None:
                        break
                    data = win.read_available()
                if data:
                    self._append(data.decode("utf-8", "replace"))
                elif not self._alive():
                    break  # 종료 후 잔여 출력까지 비운 뒤 탈출
                else:
                    time.sleep(0.1)
            return
        end = time.time() + seconds
        while time.time() < end:
            with self._io_lock:
                master = self.master
                if master is None:
                    break
                try:
                    r, _, _ = select.select([master], [], [], 0.3)
                except (OSError, ValueError):
                    break
                if not r:
                    continue
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
            if not data:
                break
            self._append(data.decode("utf-8", "replace"))

    def _alive(self) -> bool:
        with self._io_lock:
            if paths.IS_WIN:
                return self.win is not None and self.win.poll() is None
            proc = self.proc
        return proc is not None and proc.poll() is None

    def _has_session(self) -> bool:
        with self._io_lock:
            return (self.win if paths.IS_WIN else self.master) is not None

    def _returncode(self):
        with self._io_lock:
            if paths.IS_WIN:
                return self.win.poll() if self.win is not None else None
            proc = self.proc
        return proc.poll() if proc is not None else None

    def start(self, timeout: float = 8.0) -> dict:
        """격리된 정식 로그인 기동 후 authorize URL을 확보해 반환한다."""
        claude = find_claude_bin()
        if not claude:
            self.close()
            raise LimitPilotError(i18n.t("lg_no_cli"))
        env = os.environ.copy()
        for name in (
            "CLAUDE_SECURESTORAGE_CONFIG_DIR",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
            "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
            "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
            "CLAUDE_CODE_OAUTH_SCOPES",
            "CLAUDE_CODE_CUSTOM_OAUTH_URL",
            "CLAUDE_CODE_OAUTH_CLIENT_ID",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
        ):
            env.pop(name, None)
        env["CLAUDE_CONFIG_DIR"] = self.config_dir
        env["USER"] = paths.KC_USER
        if paths.IS_WIN:
            # 콘솔 폭 400: URL·토큰이 줄바꿈으로 잘리지 않게 (유닉스 winsize와 동일)
            try:
                win = conpty_win._WinConPty([claude, "auth", "login", "--claudeai"],
                                 env, cols=400, rows=60)
            except LimitPilotError:
                self.close()
                raise
            with self._io_lock:
                self.win = win
                self.pid = win.pid
        else:
            try:
                master, slave = pty.openpty()
            except OSError as e:
                self.close()
                raise LimitPilotError(i18n.t("lg_no_pty")) from e
            with self._io_lock:
                self.master = master
            # PTY 폭을 넓게 잡아 URL·토큰이 줄바꿈으로 잘리지 않게 한다
            try:
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 400, 0, 0))
            except OSError:
                pass
            try:
                proc = subprocess.Popen(
                    [claude, "auth", "login", "--claudeai"],
                    stdin=slave, stdout=slave, stderr=slave,
                    start_new_session=True, close_fds=True, env=env,
                )
            except OSError as e:
                os.close(slave)
                self.close()
                raise LimitPilotError(i18n.t("lg_spawn_failed", err=e.strerror or type(e).__name__)) from e
            os.close(slave)
            with self._io_lock:
                self.proc = proc
                self.pid = proc.pid
        end = time.time() + timeout
        while time.time() < end:
            self._drain(1.0)
            if "oauth/authorize" in self.buf:
                break
            if not self._alive():
                break
        m = re.search(r"https://\S+?oauth/authorize\S+", _strip_ansi(self.buf))
        return {"url": m.group(0) if m else None}

    def wait_for_credentials(self, timeout: float = LOGIN_TIMEOUT_S):
        """로그인 완료 후 full OAuth credentials와 실제 계정 프로필을 회수한다."""
        if not self._has_session():
            raise LimitPilotError(i18n.t("lg_no_session_restart"))
        end = time.time() + timeout
        while time.time() < end:
            self._drain(1.0)
            if not self._alive():
                break
        returncode = self._returncode()
        try:
            if returncode != 0:
                _dump_add_debug(self.buf, returncode)
                if returncode is None:
                    raise LimitPilotError(i18n.t("lg_timeout"))
                raise LimitPilotError(i18n.t("lg_incomplete"))
            return load_isolated_login(self.config_dir)
        finally:
            self.close()

    def feed(self, code: str) -> None:
        """(폴백) 콜백이 막혀 코드 붙여넣기 프롬프트에 멈춘 경우, 코드를 PTY에 주입."""
        value = (code or "").strip()
        if not value:
            raise LimitPilotError(i18n.t("lg_paste_code"))
        if len(value) > 4096:
            raise LimitPilotError(i18n.t("lg_code_too_long"))
        with self._io_lock:
            if paths.IS_WIN:
                if self.win is None:
                    raise LimitPilotError(i18n.t("lg_session_closed"))
                try:
                    # ConPTY의 Enter는 '\r' — '\n'까지 보내면 키 입력 2회가 될 수 있다
                    self.win.write(value.encode() + b"\r")
                except OSError as e:
                    raise LimitPilotError(i18n.t("lg_feed_failed")) from e
                return
            if self.master is None:
                raise LimitPilotError(i18n.t("lg_session_closed"))
            try:
                os.write(self.master, (value + "\n").encode())
            except OSError as e:
                raise LimitPilotError(i18n.t("lg_feed_failed")) from e

    def run_interactive(self, timeout: float = LOGIN_TIMEOUT_S):
        """CLI용: PTY와 사용자 stdin을 감시한 뒤 정식 credentials를 회수한다."""
        if paths.IS_WIN:
            # Windows는 stdin을 select할 수 없다 — 데몬 스레드가 stdin 라인을
            # ConPTY로 중계하고, 본류는 wait_for_credentials의 drain 루프를 쓴다
            if not self._has_session():
                raise LimitPilotError(i18n.t("lg_no_session"))
            threading.Thread(target=self._pump_stdin_win, daemon=True).start()
            return self.wait_for_credentials(timeout)
        with self._io_lock:
            master = self.master
        if master is None:
            raise LimitPilotError(i18n.t("lg_no_session"))
        end = time.time() + timeout
        while time.time() < end:
            try:
                r, _, _ = select.select([master, sys.stdin], [], [], 1.0)
            except (OSError, ValueError):
                break
            if master in r:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if data:
                    self._append(data.decode("utf-8", "replace"))
            if sys.stdin in r:
                line = sys.stdin.readline()
                if line:
                    try:
                        os.write(master, line.encode())
                    except OSError:
                        break
            if not self._alive():
                break
        with self._io_lock:
            proc = self.proc
        returncode = proc.poll() if proc is not None else None
        try:
            if returncode != 0:
                _dump_add_debug(self.buf, returncode)
                if returncode is None:
                    raise LimitPilotError(i18n.t("lg_timeout"))
                raise LimitPilotError(i18n.t("lg_incomplete"))
            return load_isolated_login(self.config_dir)
        finally:
            self.close()

    def _pump_stdin_win(self) -> None:
        """Windows CLI 폴백 입력: 사용자 stdin 라인을 ConPTY로 중계한다."""
        while True:
            try:
                line = sys.stdin.readline()
            except (OSError, ValueError):
                return
            if not line:
                return
            # feed()와 동일하게 락 안에서 쓴다 — close()가 핸들을 해제한 직후
            # 재사용된 핸들 값에 쓰는 경쟁을 차단
            with self._io_lock:
                if self.win is None:
                    return
                try:
                    self.win.write(line.rstrip("\r\n").encode() + b"\r")
                except OSError:
                    return

    def close(self) -> None:
        with self._io_lock:
            proc, master, win = self.proc, self.master, self.win
            self.proc = None
            self.pid = None
            self.master = None
            self.win = None
            should_cleanup = not self._cleaned
            self._cleaned = True
        if win is not None:
            try:
                win.close()
            except OSError:
                pass
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
        if proc is not None:
            leader_running = proc.poll() is None
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                if leader_running:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
            if leader_running:
                try:
                    proc.wait(timeout=0.75)
                except subprocess.TimeoutExpired:
                    pass
            # 리더가 먼저 끝나도 같은 그룹의 callback/브라우저 보조 자식은 남을 수 있다.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass
            if proc.poll() is None:
                try:
                    proc.wait(timeout=0.75)
                except subprocess.TimeoutExpired:
                    pass
        if should_cleanup:
            cleanup_isolated_auth(self.config_dir)
