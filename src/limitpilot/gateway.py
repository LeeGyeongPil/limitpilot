"""Third-party gateway slots (manual only): store the token as a slot, point Claude Code at
the gateway through `~/.claude/settings.json` (`env` + `apiKeyHelper`), and restore the
previous settings when switching back. The token itself never appears in settings.json.
"""

import json
import math
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from limitpilot.constants import (GATEWAY_ENV_KEYS, GATEWAY_KIND, USER_AGENT)
from limitpilot.errors import (LimitPilotError, SwitchError)
from limitpilot.daemon import deploy
from limitpilot import i18n
from limitpilot import paths
from limitpilot.policy import limits
from limitpilot import state
from limitpilot.store import creds
from limitpilot.store import filestore


def _gateway_helper_command(n: int) -> str:
    """apiKeyHelper command. The secret stays in Keychain/the private slot file."""
    # Claude Code 가 실행할 안정된 경로 — 패키지가 옮겨져도 깨지지 않는 데몬 사본(pyz)
    script = deploy.deployed_daemon_script_path()
    if not os.path.isfile(script):
        script = deploy.deploy_daemon_script()
    parts = [deploy.find_python(), script, "gateway-key", str(n)]
    if paths.IS_WIN:
        return subprocess.list2cmdline(parts)
    return " ".join(shlex.quote(part) for part in parts)


def _read_claude_settings() -> dict:
    try:
        value = filestore.read_json(paths.CLAUDE_SETTINGS_FILE)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SwitchError(i18n.t("gw_settings_unreadable", err=exc)) from exc
    if not isinstance(value, dict):
        raise SwitchError(i18n.t("gw_settings_not_object"))
    return value


def _write_claude_settings(settings: dict) -> None:
    try:
        os.makedirs(os.path.dirname(paths.CLAUDE_SETTINGS_FILE), mode=0o700, exist_ok=True)
        filestore.atomic_write_json(paths.CLAUDE_SETTINGS_FILE, settings)
    except OSError as exc:
        raise SwitchError(i18n.t("gw_settings_save_failed", err=exc)) from exc


def _gateway_env(slot: dict) -> dict:
    model = slot["modelId"]
    label = slot.get("alias") or model
    return {
        "ANTHROPIC_BASE_URL": slot["baseUrl"],
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "CLAUDE_CODE_SUBAGENT_MODEL": model,
        "ANTHROPIC_CUSTOM_MODEL_OPTION": model,
        "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": label,
        "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "Third-party model via LimitPilot",
        # Unknown non-Claude models may reject Claude's thinking request field.
        "MAX_THINKING_TOKENS": "0",
    }


def gateway_config_is_active(st: dict, n: int) -> bool:
    slot = state.slot_of(st, n)
    if not limits.is_gateway_slot(slot):
        return False
    try:
        settings = _read_claude_settings()
    except SwitchError:
        return False
    env = settings.get("env") or {}
    expected = _gateway_env(slot)
    return (isinstance(env, dict)
            and all(env.get(key) == value for key, value in expected.items())
            and settings.get("apiKeyHelper") == _gateway_helper_command(n))


def activate_gateway_settings(st: dict, n: int) -> None:
    """Persist gateway routing while preserving unrelated Claude settings.

    Credentials are supplied through apiKeyHelper, never embedded in settings.json.
    """
    slot = state.slot_of(st, n)
    if not limits.is_gateway_slot(slot):
        raise SwitchError(i18n.t("gw_not_profile", n=n))
    if not creds.kc_read_slot(n):
        raise SwitchError(i18n.t("gw_no_token", n=n))
    settings = _read_claude_settings()
    env = settings.get("env") or {}
    if not isinstance(env, dict):
        raise SwitchError(i18n.t("gw_env_not_object"))
    # Never copy an existing plaintext secret into state.json for later restoration.
    for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        if env.get(key):
            raise SwitchError(i18n.t("gw_plain_secret", key=key))
    if "gatewayRestore" not in st:
        st["gatewayRestore"] = {
            "settingsFilePresent": os.path.exists(paths.CLAUDE_SETTINGS_FILE),
            "env": {key: {"present": key in env, "value": env.get(key)}
                    for key in GATEWAY_ENV_KEYS},
            "apiKeyHelper": {
                "present": "apiKeyHelper" in settings,
                "value": settings.get("apiKeyHelper"),
            },
        }
        state.save_state(st)
    env.update(_gateway_env(slot))
    settings["env"] = env
    settings["apiKeyHelper"] = _gateway_helper_command(n)
    _write_claude_settings(settings)
    st["activeSlot"] = n
    st["lastSwitchAt"] = time.time()
    st["lastSwitchTo"] = n
    state.save_state(st)


def deactivate_gateway_settings(st: dict) -> None:
    restore = st.get("gatewayRestore")
    if not isinstance(restore, dict):
        return
    active = state.slot_of(st, st.get("activeSlot"))
    inherited = [key for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
                 if os.environ.get(key)]
    if limits.is_gateway_slot(active) and inherited:
        raise SwitchError(i18n.t("gw_shell_env", names=" ".join(inherited)))
    settings = _read_claude_settings()
    env = settings.get("env") or {}
    if not isinstance(env, dict):
        raise SwitchError(i18n.t("gw_env_not_object"))
    for key, previous in (restore.get("env") or {}).items():
        if previous.get("present"):
            env[key] = previous.get("value")
        else:
            env.pop(key, None)
    if env:
        settings["env"] = env
    else:
        settings.pop("env", None)
    helper = restore.get("apiKeyHelper") or {}
    if helper.get("present"):
        settings["apiKeyHelper"] = helper.get("value")
    else:
        settings.pop("apiKeyHelper", None)
    _write_claude_settings(settings)
    if not restore.get("settingsFilePresent") and not settings:
        try:
            os.remove(paths.CLAUDE_SETTINGS_FILE)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SwitchError(i18n.t("gw_tmp_cleanup", err=exc)) from exc
    st.pop("gatewayRestore", None)
    state.save_state(st)


def _load_gateway_env(path: str) -> dict:
    """Read a local KEY=VALUE file without executing shell syntax."""
    values = {}
    path = os.path.abspath(path)
    try:
        with open(path, encoding="utf-8") as handle:
            for number, raw in enumerate(handle, 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if "=" not in line:
                    raise LimitPilotError(i18n.t("gw_env_line", n=number))
                key, value = line.split("=", 1)
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    except FileNotFoundError:
        # 없는 파일 — 무엇을 담은 파일을 어디에 만들지(또는 --env-file) 알려 준다
        raise LimitPilotError(i18n.t("gw_env_missing", path=path)) from None
    except OSError as exc:
        raise LimitPilotError(i18n.t("gw_env_read", err=exc)) from exc
    return values


def validate_gateway_profile(base_url, model, token, auth_mode="bearer", alias=None):
    """Validate and normalize gateway input without persisting or echoing its token."""
    if not all(isinstance(v, str) for v in (base_url, model, token, auth_mode)):
        raise LimitPilotError(i18n.t("gw_not_string"))
    base_url = base_url.strip().rstrip("/")
    model = model.strip()
    auth_mode = auth_mode.strip().lower()
    parsed = urllib.parse.urlparse(base_url)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise LimitPilotError(i18n.t("gw_bad_url"))
    if not model or len(model) > 200 or any(ch.isspace() for ch in model):
        raise LimitPilotError(i18n.t("gw_bad_model"))
    if auth_mode not in ("bearer", "api-key"):
        raise LimitPilotError(i18n.t("gw_bad_auth"))
    if not token or len(token) > 8192 or "\n" in token or "\r" in token:
        raise LimitPilotError(i18n.t("gw_bad_token"))
    alias = (alias or model).strip() if isinstance(alias, str) or alias is None else ""
    if not alias or len(alias) > 80:
        raise LimitPilotError(i18n.t("gw_bad_alias"))
    return base_url, model, token, auth_mode, alias


def validate_gateway_usage_config(input_token_limit=0, output_token_limit=0,
                                  input_price_per_million=0,
                                  output_price_per_million=0, cost_limit=0):
    """Normalize non-secret gateway usage and billing settings.

    Token limits are whole-token counters. A zero limit means unlimited. Prices
    and the optional combined cost limit are USD amounts and must be finite.
    """
    values = {
        "inputTokenLimit": input_token_limit,
        "outputTokenLimit": output_token_limit,
        "inputPricePerMillion": input_price_per_million,
        "outputPricePerMillion": output_price_per_million,
        "costLimit": cost_limit,
    }
    normalized = {}
    for key, value in values.items():
        if isinstance(value, bool):
            raise LimitPilotError(i18n.t("gw_limit_number"))
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            raise LimitPilotError(i18n.t("gw_limit_number")) from None
        if not math.isfinite(number) or number < 0:
            raise LimitPilotError(i18n.t("gw_limit_number"))
        if key.endswith("TokenLimit"):
            if not number.is_integer() or number > 9_007_199_254_740_991:
                raise LimitPilotError(i18n.t("gw_limit_int"))
            normalized[key] = int(number)
        else:
            normalized[key] = number
    return normalized


def probe_gateway_connection(base_url, model, token, auth_mode="bearer") -> None:
    """One small Anthropic Messages request; errors never include the credential."""
    base_url, model, token, auth_mode, _ = validate_gateway_profile(
        base_url, model, token, auth_mode, alias=model)
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "User-Agent": USER_AGENT,
    }
    if auth_mode == "bearer":
        headers["Authorization"] = "Bearer " + token
    else:
        headers["x-api-key"] = token
    body = json.dumps({
        "model": model, "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }).encode("utf-8")
    request = urllib.request.Request(base_url + "/v1/messages", data=body,
                                     headers=headers, method="POST")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        # Never forward a gateway credential to a redirect target.
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            data = json.loads(response.read(1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise LimitPilotError(i18n.t("gw_verify_http", code=exc.code)) from None
    except (urllib.error.URLError, TimeoutError):
        raise LimitPilotError(i18n.t("gw_unreachable")) from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise LimitPilotError(i18n.t("gw_not_json")) from None
    if not isinstance(data, dict) or data.get("type") != "message" or not isinstance(data.get("content"), list):
        raise LimitPilotError(i18n.t("gw_not_messages"))


def register_gateway_slot(st: dict, base_url, model, token, auth_mode="bearer", alias=None,
                          usage_config=None):
    """Store a validated gateway profile. Caller owns state_lock; returns (n,label,is_new).

    ``alias`` None means "not given": a gateway that is already registered keeps the alias
    the user chose earlier, a new one is named after its model.
    """
    alias_given = isinstance(alias, str) and bool(alias.strip())
    base_url, model, token, auth_mode, alias = validate_gateway_profile(
        base_url, model, token, auth_mode, alias)
    n = next((slot_n for slot_n in state.registered_slots(st)
              if limits.is_gateway_slot(state.slot_of(st, slot_n))
              and state.slot_of(st, slot_n).get("baseUrl") == base_url
              and state.slot_of(st, slot_n).get("modelId") == model), None)
    is_new = n is None
    if n is None:
        n = state.next_free_slot(st)
    previous = state.slot_of(st, n) or {}
    if not alias_given and previous.get("alias"):
        alias = previous["alias"]   # 다시 등록해도 사용자가 바꾼 별칭은 그대로
    raw_config = (previous.get("gatewayUsageConfig") or {}) if usage_config is None else usage_config
    usage_config = validate_gateway_usage_config(
        raw_config.get("inputTokenLimit", 0), raw_config.get("outputTokenLimit", 0),
        raw_config.get("inputPricePerMillion", 0),
        raw_config.get("outputPricePerMillion", 0), raw_config.get("costLimit", 0))
    creds.kc_write_slot(n, token)
    st["slots"][str(n)] = {
        "alias": alias,
        "email": None,
        "orgName": "Third-party gateway",
        "addedAt": previous.get("addedAt") or datetime.now(timezone.utc).isoformat(),
        "baseUrl": base_url,
        "modelId": model,
        "authMode": auth_mode,
        "enabled": True,
        "quarantine": None,
        "kind": GATEWAY_KIND,
        "gatewayUsageConfig": usage_config,
    }
    if isinstance(previous.get("tokenUsage"), dict):
        st["slots"][str(n)]["tokenUsage"] = dict(previous["tokenUsage"])
    state.save_state(st)
    if st.get("activeSlot") == n and st.get("gatewayRestore") is not None:
        activate_gateway_settings(st, n)
    return n, alias, is_new


def update_gateway_slot(st: dict, n: int, base_url, model, token,
                        auth_mode="bearer", alias=None, usage_config=None):
    """Update one gateway slot in place. Caller owns state_lock."""
    current = state.slot_of(st, n)
    if not limits.is_gateway_slot(current):
        raise LimitPilotError(i18n.t("gw_not_third_party", n=n))
    base_url, model, token, auth_mode, alias = validate_gateway_profile(
        base_url, model, token, auth_mode, alias)
    duplicate = next((slot_n for slot_n in state.registered_slots(st)
                      if slot_n != n and limits.is_gateway_slot(state.slot_of(st, slot_n))
                      and state.slot_of(st, slot_n).get("baseUrl") == base_url
                      and state.slot_of(st, slot_n).get("modelId") == model), None)
    if duplicate is not None:
        raise LimitPilotError(i18n.t("gw_duplicate", n=duplicate))
    raw_config = (current.get("gatewayUsageConfig") or {}) if usage_config is None else usage_config
    usage_config = validate_gateway_usage_config(
        raw_config.get("inputTokenLimit", 0), raw_config.get("outputTokenLimit", 0),
        raw_config.get("inputPricePerMillion", 0),
        raw_config.get("outputPricePerMillion", 0), raw_config.get("costLimit", 0))
    creds.kc_write_slot(n, token)
    st["slots"][str(n)] = {
        "alias": alias,
        "email": None,
        "orgName": "Third-party gateway",
        "addedAt": current.get("addedAt") or datetime.now(timezone.utc).isoformat(),
        "baseUrl": base_url,
        "modelId": model,
        "authMode": auth_mode,
        "enabled": True,
        "quarantine": None,
        "kind": GATEWAY_KIND,
        "gatewayUsageConfig": usage_config,
    }
    if current.get("modelId") == model and isinstance(current.get("tokenUsage"), dict):
        st["slots"][str(n)]["tokenUsage"] = dict(current["tokenUsage"])
    state.save_state(st)
    if st.get("activeSlot") == n and st.get("gatewayRestore") is not None:
        activate_gateway_settings(st, n)
    return n, alias
