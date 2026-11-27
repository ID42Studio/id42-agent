#!/usr/bin/env python3
"""Render and inspect id42-agent settings.

Reads the id42-agent env file and writes nanobot's config.json shape.
Does not print tokens or other secret values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ENV_KEYS = (
    "TZ",
    "OLLAMA_BASE_URL",
    "OLLAMA_MODEL",
    "TELEGRAM_ENABLED",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USERS",
    "NANOBOT_WEB_TOKEN",
    "ID42_WORK_ENABLED",
    "ID42_WORK_FACTORY",
    "ID42_WORK_PYTHONPATH",
)

# Matches id42_work.mcp.server.load_factory at the pinned id42-work commit.
_FACTORY_RE = re.compile(r"\A[A-Za-z_][\w.]*:[A-Za-z_]\w*\Z")
# Durable factory. id42_agent.mcp_stub:server is the empty in-memory factory.
_DEFAULT_WORK_FACTORY = "id42_agent.mcp_host:server"
_WORK_TOOL_TIMEOUT = 120
_IMAGE_PYTHONPATH = "/opt/id42-agent"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DATA_CONFIG = Path("/home/nanobot/.nanobot/config.json")
_WORKSPACE = Path("/home/nanobot/.nanobot/workspace")


class ConfigError(Exception):
    """Invalid id42-agent settings."""


def _fail(message: str) -> None:
    print(f"id42-agent: {message}", file=sys.stderr)
    raise SystemExit(2)


def parse_env_text(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines. Unknown keys are ignored by callers."""
    values: dict[str, str] = {}
    for line_number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ConfigError(f"invalid .env line {line_number}")
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not _KEY_RE.match(key):
            raise ConfigError(f"invalid .env key on line {line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def load_env_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from exc
    return parse_env_text(text)


def _pick(env: dict[str, str]) -> dict[str, str]:
    return {key: env.get(key, "") for key in ENV_KEYS}


def _parse_bool(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false")


def _validate_timezone(value: str) -> str:
    timezone = value.strip()
    if not timezone or any(char.isspace() for char in timezone):
        raise ConfigError("TZ must be an IANA timezone name such as America/Sao_Paulo")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(timezone)
    except Exception as exc:
        raise ConfigError(f"TZ is not a known IANA timezone: {timezone}") from exc
    return timezone


def _validate_ollama_url(value: str) -> str:
    raw = value.strip()
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise ConfigError("OLLAMA_BASE_URL must be an http(s) URL with no query string")
    if parsed.path.rstrip("/") != "/v1":
        raise ConfigError(
            "OLLAMA_BASE_URL must end with /v1 "
            "(nanobot's Ollama default_api_base includes that path)"
        )
    return raw.rstrip("/")


def _allowlist(raw: str) -> list[str]:
    entries = [part.strip() for part in raw.split(",")]
    entries = [entry for entry in entries if entry]
    if not entries:
        raise ConfigError(
            "TELEGRAM_ALLOWED_USERS must list at least one Telegram user id or username"
        )
    if any(entry == "*" for entry in entries):
        raise ConfigError(
            "TELEGRAM_ALLOWED_USERS rejects *: a personal bot needs an explicit allowlist"
        )
    cleaned: list[str] = []
    for entry in entries:
        if any(char.isspace() for char in entry):
            raise ConfigError("TELEGRAM_ALLOWED_USERS entries cannot contain whitespace")
        if entry.startswith("@"):
            raise ConfigError(
                "strip the leading @ from TELEGRAM_ALLOWED_USERS entries "
                "(nanobot matches the id or username without @)"
            )
        cleaned.append(entry)
    return cleaned


def _factory_error(value: str) -> str | None:
    """Return an error when value is not module:callable, else None."""
    module = value.split(":", 1)[0]
    if not _FACTORY_RE.fullmatch(value) or ".." in value or module.endswith("."):
        return (
            "ID42_WORK_FACTORY must be module:callable, "
            "for example id42_agent.mcp_host:server"
        )
    return None


def _work_factory(raw: str) -> str:
    factory = raw.strip() or _DEFAULT_WORK_FACTORY
    error = _factory_error(factory)
    if error:
        raise ConfigError(error)
    return factory


def _work_pythonpath(raw: str) -> str:
    """Return the MCP process PYTHONPATH. Empty means inherit the image default."""
    value = raw.strip()
    if not value:
        return ""
    if any(char.isspace() for char in value):
        raise ConfigError("ID42_WORK_PYTHONPATH cannot contain whitespace")
    cleaned: list[str] = []
    for part in value.split(":"):
        if not part.startswith("/") or part == "/":
            raise ConfigError(
                "ID42_WORK_PYTHONPATH must be a colon-separated list of absolute paths"
            )
        if ".." in part.split("/"):
            raise ConfigError("ID42_WORK_PYTHONPATH cannot contain ..")
        if part not in cleaned:
            cleaned.append(part)
    if _IMAGE_PYTHONPATH not in cleaned:
        cleaned.insert(0, _IMAGE_PYTHONPATH)
    return ":".join(cleaned)


def _work_server(env: dict[str, str]) -> dict[str, object] | None:
    """Stdio MCP server block, or None when ID42 Work is left off."""
    enabled = _parse_bool("ID42_WORK_ENABLED", env["ID42_WORK_ENABLED"] or "false")
    factory_raw = env["ID42_WORK_FACTORY"]
    pythonpath_raw = env["ID42_WORK_PYTHONPATH"]
    if not enabled:
        if factory_raw.strip():
            _work_factory(factory_raw)
        if pythonpath_raw.strip():
            _work_pythonpath(pythonpath_raw)
        return None
    server: dict[str, object] = {
        "command": "id42-work",
        "args": ["mcp", "serve", "--factory", _work_factory(factory_raw)],
        "toolTimeout": _WORK_TOOL_TIMEOUT,
        # nanobot's stdio client inherits PATH and HOME, not PYTHONPATH.
        # The default factory is imported from /opt/id42-agent.
        "env": {"PYTHONPATH": _work_pythonpath(pythonpath_raw) or _IMAGE_PYTHONPATH},
    }
    return server


def mcp_server_errors(server: object) -> list[str]:
    """Reject anything other than the stdio ``id42-work mcp serve`` command."""
    label = "tools.mcpServers.id42-work"
    if not isinstance(server, dict):
        return [f"{label} is not an object"]
    errors: list[str] = []
    if server.get("command") != "id42-work":
        errors.append(f"{label}.command must be id42-work")
    url = server.get("url")
    if isinstance(url, str) and url.strip():
        errors.append(f"{label}.url must stay empty; the server is stdio only")
    elif url not in (None, ""):
        errors.append(f"{label}.url must stay empty; the server is stdio only")
    transport = server.get("type")
    if transport not in (None, "stdio"):
        errors.append(f"{label}.type must be stdio or omitted")
    args = server.get("args")
    if (
        not isinstance(args, list)
        or len(args) != 4
        or args[:3] != ["mcp", "serve", "--factory"]
        or not isinstance(args[3], str)
        or _factory_error(args[3])
    ):
        errors.append(f"{label}.args must be mcp serve --factory module:callable")
    elif "--http" in args:
        errors.append(f"{label} must not pass --http")
    timeout = server.get("toolTimeout", _WORK_TOOL_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        errors.append(f"{label}.toolTimeout must be a positive integer")
    extra_env = server.get("env")
    if extra_env is not None and (
        not isinstance(extra_env, dict)
        or any(not isinstance(key, str) or not isinstance(item, str) for key, item in extra_env.items())
    ):
        errors.append(f"{label}.env must be a map of strings")
        extra_env = None
    factory = args[3] if isinstance(args, list) and len(args) == 4 else ""
    pythonpath = ""
    if isinstance(extra_env, dict):
        pythonpath = extra_env.get("PYTHONPATH", "")
    if isinstance(factory, str) and factory.startswith("id42_agent.") and (
        not isinstance(pythonpath, str) or _IMAGE_PYTHONPATH not in pythonpath.split(":")
    ):
        errors.append(
            f"{label}.env.PYTHONPATH must include {_IMAGE_PYTHONPATH} "
            "so the image factory can be imported"
        )
    if isinstance(pythonpath, str) and pythonpath:
        for part in pythonpath.split(":"):
            if not part.startswith("/") or ".." in part.split("/"):
                errors.append(f"{label}.env.PYTHONPATH must be absolute paths")
                break
    return errors


def mcp_config_errors(document: dict[str, object], *, enabled: bool) -> list[str]:
    """Check tools.mcpServers.id42-work. Absence is an error only when enabled."""
    tools = document.get("tools")
    if not isinstance(tools, dict) or "mcpServers" not in tools:
        if enabled:
            return ["ID42_WORK_ENABLED is true but tools.mcpServers.id42-work is missing"]
        return []
    servers = tools.get("mcpServers")
    if not isinstance(servers, dict):
        return ["tools.mcpServers is not an object"]
    server = servers.get("id42-work")
    if server is None:
        if enabled:
            return ["ID42_WORK_ENABLED is true but tools.mcpServers.id42-work is missing"]
        return []
    return mcp_server_errors(server)


def build_config(env: dict[str, str]) -> dict[str, object]:
    """Return the first-boot config.json object. Raises ConfigError when invalid."""
    picked = _pick(env)
    timezone = _validate_timezone(picked["TZ"])
    api_base = _validate_ollama_url(picked["OLLAMA_BASE_URL"])
    model = picked["OLLAMA_MODEL"].strip()
    if not model or any(char.isspace() for char in model):
        raise ConfigError("OLLAMA_MODEL is required and cannot contain whitespace")
    web_token = picked["NANOBOT_WEB_TOKEN"].strip()
    if not web_token:
        raise ConfigError("NANOBOT_WEB_TOKEN is empty; run ensure-token first")
    if any(char.isspace() for char in web_token):
        raise ConfigError("NANOBOT_WEB_TOKEN cannot contain whitespace")

    telegram_enabled = _parse_bool("TELEGRAM_ENABLED", picked["TELEGRAM_ENABLED"] or "false")
    channels: dict[str, object] = {
        "websocket": {
            "enabled": True,
            "host": "0.0.0.0",
            "port": 8765,
            "tokenIssueSecret": web_token,
            "websocketRequiresToken": True,
        }
    }
    if telegram_enabled:
        token = picked["TELEGRAM_BOT_TOKEN"].strip()
        if not token or any(char.isspace() for char in token):
            raise ConfigError("TELEGRAM_BOT_TOKEN is required when TELEGRAM_ENABLED=true")
        channels["telegram"] = {
            "enabled": True,
            "token": token,
            "allowFrom": _allowlist(picked["TELEGRAM_ALLOWED_USERS"]),
            "mode": "polling",
        }

    tools: dict[str, object] = {
        "restrictToWorkspace": True,
        "exec": {
            "enable": False,
        },
    }
    work_server = _work_server(picked)
    if work_server is not None:
        tools["mcpServers"] = {"id42-work": work_server}

    return {
        "agents": {
            "defaults": {
                "workspace": "/home/nanobot/.nanobot/workspace",
                "modelPreset": "primary",
                "timezone": timezone,
                "timezoneMode": "manual",
            }
        },
        "providers": {
            "ollama": {
                "apiBase": api_base,
            }
        },
        "modelPresets": {
            "primary": {
                "provider": "ollama",
                "model": model,
                "maxTokens": 4096,
                "contextWindowTokens": 32768,
            }
        },
        "gateway": {
            "host": "0.0.0.0",
            "port": 18790,
        },
        "channels": channels,
        "tools": tools,
    }


def _write_env_value(path: Path, key: str, value: str) -> None:
    text = path.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    lines = text.splitlines(keepends=True)
    replaced = False
    rendered = f"{key}={value}\n"
    output: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("#") or "=" not in stripped:
            output.append(raw if raw.endswith("\n") else raw + "\n")
            continue
        current = stripped.split("=", 1)[0].strip()
        if current == key:
            output.append(rendered)
            replaced = True
        else:
            output.append(raw if raw.endswith("\n") else raw + "\n")
    if not replaced:
        output.append(rendered)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(output), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def ensure_token(path: Path) -> None:
    env = load_env_file(path)
    current = env.get("NANOBOT_WEB_TOKEN", "").strip()
    if current:
        if any(char.isspace() for char in current):
            raise ConfigError("NANOBOT_WEB_TOKEN cannot contain whitespace")
        print(f"NANOBOT_WEB_TOKEN already set in {path}")
        return
    _write_env_value(path, "NANOBOT_WEB_TOKEN", secrets.token_hex(32))
    print(f"Generated NANOBOT_WEB_TOKEN and stored it in {path}")


def render(path: Path, output: Path) -> None:
    config = build_config(load_env_file(path))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    output.chmod(0o600)
    print(f"Rendered config to {output}")


def _load_json(path: Path) -> dict[str, object]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} is not a JSON object")
    return loaded


def _dig(document: dict[str, object], *keys: str) -> object:
    current: object = document
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise ConfigError(f"missing {'.'.join(keys)}")
        current = current[key]
    return current


def assert_volume() -> None:
    """Check the first-boot file inside the data volume. Prints no secrets."""
    document = _load_json(_DATA_CONFIG)
    errors: list[str] = []

    def expect(actual: object, expected: object, label: str) -> None:
        if actual != expected:
            errors.append(f"{label} is {actual!r}")

    try:
        expect(_dig(document, "gateway", "host"), "0.0.0.0", "gateway.host")
        expect(_dig(document, "gateway", "port"), 18790, "gateway.port")
        expect(_dig(document, "channels", "websocket", "enabled"), True, "channels.websocket.enabled")
        expect(_dig(document, "channels", "websocket", "host"), "0.0.0.0", "channels.websocket.host")
        expect(_dig(document, "channels", "websocket", "port"), 8765, "channels.websocket.port")
        expect(
            _dig(document, "channels", "websocket", "websocketRequiresToken"),
            True,
            "channels.websocket.websocketRequiresToken",
        )
        secret = _dig(document, "channels", "websocket", "tokenIssueSecret")
        if not isinstance(secret, str) or not secret.strip():
            errors.append("channels.websocket.tokenIssueSecret is empty")
        api_base = _dig(document, "providers", "ollama", "apiBase")
        if not isinstance(api_base, str) or not api_base.rstrip("/").endswith("/v1"):
            errors.append("providers.ollama.apiBase must end with /v1")
        expect(_dig(document, "modelPresets", "primary", "provider"), "ollama", "modelPresets.primary.provider")
        model = _dig(document, "modelPresets", "primary", "model")
        if not isinstance(model, str) or not model.strip():
            errors.append("modelPresets.primary.model is empty")
        expect(_dig(document, "modelPresets", "primary", "maxTokens"), 4096, "modelPresets.primary.maxTokens")
        expect(
            _dig(document, "modelPresets", "primary", "contextWindowTokens"),
            32768,
            "modelPresets.primary.contextWindowTokens",
        )
        expect(_dig(document, "agents", "defaults", "modelPreset"), "primary", "agents.defaults.modelPreset")
        expect(
            _dig(document, "agents", "defaults", "workspace"),
            "/home/nanobot/.nanobot/workspace",
            "agents.defaults.workspace",
        )
        expect(_dig(document, "agents", "defaults", "timezoneMode"), "manual", "agents.defaults.timezoneMode")
        timezone = _dig(document, "agents", "defaults", "timezone")
        if not isinstance(timezone, str) or not timezone.strip():
            errors.append("agents.defaults.timezone is empty")
        expect(_dig(document, "tools", "restrictToWorkspace"), True, "tools.restrictToWorkspace")
        expect(_dig(document, "tools", "exec", "enable"), False, "tools.exec.enable")
        errors.extend(mcp_config_errors(document, enabled=False))
        channels = document.get("channels")
        telegram = channels.get("telegram") if isinstance(channels, dict) else None
        if telegram is not None:
            if not isinstance(telegram, dict):
                errors.append("channels.telegram is not an object")
            else:
                expect(telegram.get("enabled"), True, "channels.telegram.enabled")
                token = telegram.get("token")
                if not isinstance(token, str) or not token.strip():
                    errors.append("channels.telegram.token is empty")
                expect(telegram.get("mode"), "polling", "channels.telegram.mode")
                allow = telegram.get("allowFrom")
                if not isinstance(allow, list) or not allow or "*" in allow:
                    errors.append("channels.telegram.allowFrom is missing an explicit allowlist")
        for name in ("AGENTS.md", "SOUL.md", "USER.md", "skills/README.md"):
            if not (_WORKSPACE / name).is_file():
                errors.append(f"workspace seed missing: {name}")
    except ConfigError as exc:
        errors.append(str(exc))

    if errors:
        for item in errors:
            print(f"id42-agent: {item}", file=sys.stderr)
        raise SystemExit(2)

    secret = _dig(document, "channels", "websocket", "tokenIssueSecret")
    digest = hashlib.sha256(str(secret).encode("utf-8")).hexdigest()
    print(f"config-ok token_sha256={digest}")


def _secret_values(document: dict[str, object]) -> list[str]:
    found: list[str] = []
    channels = document.get("channels")
    if not isinstance(channels, dict):
        return found
    websocket = channels.get("websocket")
    telegram = channels.get("telegram")
    for section, key in ((websocket, "tokenIssueSecret"), (telegram, "token")):
        if isinstance(section, dict) and isinstance(section.get(key), str):
            secret = section[key].strip()
            if secret:
                found.append(secret)
    return found


def validate_file(path: Path) -> None:
    """Load a config file with nanobot itself. Secret values are stripped from errors."""
    document = _load_json(path)
    try:
        from nanobot.config.loader import load_config
    except Exception as exc:
        raise ConfigError(f"nanobot is not importable: {exc}") from exc
    try:
        load_config(path)
    except Exception as exc:
        text = str(exc)
        for secret in _secret_values(document):
            text = text.replace(secret, "[redacted]")
        raise ConfigError(f"nanobot rejected config.json: {text}") from exc
    print("config-valid")


def validate_volume() -> None:
    """Load the data-volume config.json with nanobot itself."""
    validate_file(_DATA_CONFIG)


def assert_mcp(config_path: Path, *, enabled: bool) -> None:
    """Check the id42-work MCP block. Prints no secrets."""
    document = _load_json(config_path)
    errors = mcp_config_errors(document, enabled=enabled)
    if errors:
        for item in errors:
            print(f"id42-agent: {item}", file=sys.stderr)
        raise SystemExit(2)
    print("mcp-config-ok")


def public_summary(env_file: Path | None, config_path: Path | None) -> None:
    """Print non-secret facts for doctor.sh."""
    summary: dict[str, object] = {}
    if env_file is not None:
        picked = _pick(load_env_file(env_file))
        summary["env"] = {
            "TZ": picked["TZ"],
            "OLLAMA_BASE_URL": picked["OLLAMA_BASE_URL"],
            "OLLAMA_MODEL": picked["OLLAMA_MODEL"],
            "TELEGRAM_ENABLED": picked["TELEGRAM_ENABLED"],
            "telegram_token_set": bool(picked["TELEGRAM_BOT_TOKEN"].strip()),
            "telegram_allowlist_set": bool(picked["TELEGRAM_ALLOWED_USERS"].strip()),
            "web_token_set": bool(picked["NANOBOT_WEB_TOKEN"].strip()),
            "ID42_WORK_ENABLED": picked["ID42_WORK_ENABLED"] or "false",
            "ID42_WORK_FACTORY": picked["ID42_WORK_FACTORY"],
            "id42_work_pythonpath_set": bool(picked["ID42_WORK_PYTHONPATH"].strip()),
        }
    if config_path is not None and config_path.is_file():
        document = _load_json(config_path)
        channels = document.get("channels")
        telegram = channels.get("telegram") if isinstance(channels, dict) else None
        allow = telegram.get("allowFrom") if isinstance(telegram, dict) else None
        token = telegram.get("token") if isinstance(telegram, dict) else ""
        summary["config"] = {
            "ollama_api_base": _dig(document, "providers", "ollama", "apiBase")
            if "providers" in document
            else None,
            "model": _dig(document, "modelPresets", "primary", "model")
            if "modelPresets" in document
            else None,
            "restrict_to_workspace": _dig(document, "tools", "restrictToWorkspace")
            if "tools" in document
            else None,
            "exec_enable": _dig(document, "tools", "exec", "enable") if "tools" in document else None,
            "telegram_enabled": bool(isinstance(telegram, dict) and telegram.get("enabled") is True),
            "telegram_token_set": bool(isinstance(token, str) and token.strip()),
            "telegram_allow_from": allow if isinstance(allow, list) else [],
            "timezone": _dig(document, "agents", "defaults", "timezone")
            if "agents" in document
            else None,
            "id42_work_configured": bool(mcp_config_errors(document, enabled=True) == []),
        }
    print(json.dumps(summary, indent=2))


def _redact(text: str, secret: str) -> str:
    if secret:
        return text.replace(secret, "[redacted]")
    return text


def telegram_getme(token: str) -> dict[str, object]:
    """Call Telegram getMe. The token is never included in the result or errors."""
    cleaned = token.strip()
    if not cleaned:
        raise ConfigError("Telegram token is empty")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{cleaned}/getMe",
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ConfigError(
            f"Telegram getMe returned HTTP {exc.code}: {_redact(detail, cleaned)[:300]}"
        ) from exc
    except urllib.error.URLError as exc:
        reason = _redact(str(exc.reason), cleaned)
        raise ConfigError(f"Telegram getMe failed: {reason}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ConfigError("Telegram getMe returned non-JSON") from exc
    if status != 200 or not isinstance(payload, dict) or payload.get("ok") is not True:
        raise ConfigError("Telegram getMe rejected the token")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ConfigError("Telegram getMe returned no bot identity")
    return {
        "ok": True,
        "id": result.get("id"),
        "username": result.get("username"),
        "is_bot": result.get("is_bot"),
    }


def telegram_getme_from_env(path: Path) -> None:
    token = load_env_file(path).get("TELEGRAM_BOT_TOKEN", "")
    print(json.dumps(telegram_getme(token)))


def telegram_getme_from_config(path: Path) -> None:
    document = _load_json(path)
    channels = document.get("channels")
    telegram = channels.get("telegram") if isinstance(channels, dict) else None
    token = telegram.get("token") if isinstance(telegram, dict) else ""
    if not isinstance(token, str):
        raise ConfigError("channels.telegram.token is missing")
    print(json.dumps(telegram_getme(token)))


def ollama_models(base_url: str) -> list[str]:
    """GET {apiBase}/models and return model ids. No secrets are printed."""
    url = _validate_ollama_url(base_url) + "/models"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        raise ConfigError(f"Ollama models request returned HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise ConfigError(f"Ollama is not reachable at {url}: {exc.reason}") from exc
    if status != 200:
        raise ConfigError(f"Ollama models request returned HTTP {status} for {url}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Ollama at {url} did not return JSON") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ConfigError(f"Ollama at {url} has no data list")
    names: list[str] = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            names.append(item["id"])
    return names


def ollama_probe(base_url: str, model: str) -> None:
    names = ollama_models(base_url)
    print("ollama-models:")
    for name in names:
        print(f"  {name}")
    if not model.strip():
        raise ConfigError("configured model is empty")
    if not _model_present(model.strip(), names):
        raise ConfigError(
            f"configured model {model.strip()!r} is not in the Ollama /v1/models list"
        )
    print(f"ollama-model-ok: {model.strip()}")


def _model_present(model: str, names: list[str]) -> bool:
    candidates = {model, f"{model}:latest"}
    for name in names:
        if name in candidates or name.split(":", 1)[0] == model:
            return True
    return False


def _command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="id42-agent config helpers")
    sub = parser.add_subparsers(dest="command", required=True)

    ensure = sub.add_parser("ensure-token")
    ensure.add_argument("--env-file", type=Path, required=True)

    render_parser = sub.add_parser("render")
    render_parser.add_argument("--env-file", type=Path, required=True)
    render_parser.add_argument("--output", type=Path, required=True)

    summary = sub.add_parser("summary")
    summary.add_argument("--env-file", type=Path)
    summary.add_argument("--config", type=Path)

    getme_env = sub.add_parser("telegram-getme-env")
    getme_env.add_argument("--env-file", type=Path, required=True)

    getme_config = sub.add_parser("telegram-getme-config")
    getme_config.add_argument("--config", type=Path, required=True)

    probe = sub.add_parser("ollama-probe")
    probe.add_argument("--base-url", required=True)
    probe.add_argument("--model", required=True)

    sub.add_parser("assert-volume")
    sub.add_parser("validate-volume")

    validate_file_parser = sub.add_parser("validate-file")
    validate_file_parser.add_argument("--config", type=Path, required=True)

    mcp_parser = sub.add_parser("assert-mcp")
    mcp_parser.add_argument("--config", type=Path, default=_DATA_CONFIG)
    mcp_parser.add_argument("--enabled", choices=("true", "false"))
    mcp_parser.add_argument("--env-file", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "ensure-token":
            ensure_token(args.env_file)
        elif args.command == "render":
            render(args.env_file, args.output)
        elif args.command == "summary":
            public_summary(args.env_file, args.config)
        elif args.command == "telegram-getme-env":
            telegram_getme_from_env(args.env_file)
        elif args.command == "telegram-getme-config":
            telegram_getme_from_config(args.config)
        elif args.command == "ollama-probe":
            ollama_probe(args.base_url, args.model)
        elif args.command == "assert-volume":
            assert_volume()
        elif args.command == "validate-volume":
            validate_volume()
        elif args.command == "validate-file":
            validate_file(args.config)
        elif args.command == "assert-mcp":
            if args.env_file is not None:
                raw = load_env_file(args.env_file).get("ID42_WORK_ENABLED", "false")
                enabled = _parse_bool("ID42_WORK_ENABLED", raw or "false")
            elif args.enabled is not None:
                enabled = args.enabled == "true"
            else:
                raise ConfigError("assert-mcp needs --enabled or --env-file")
            assert_mcp(args.config, enabled=enabled)
        else:
            parser.error("unknown command")
    except ConfigError as exc:
        print(f"id42-agent: {exc}", file=sys.stderr)
        return 2
    return 0


def main() -> None:
    raise SystemExit(_command(sys.argv[1:]))


if __name__ == "__main__":
    main()
