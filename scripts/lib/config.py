#!/usr/bin/env python3
"""Render and inspect id42-agent settings.

Reads the id42-agent env file and writes nanobot's config.json shape.
Does not print tokens or other secret values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
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
    "ID42_WORK_AUTHORITY_KEY_PATH",
    "ID42_WORK_STORAGE",
    "ID42_WORK_DATABASE_URL",
    "ID42_WORK_DATABASE_URL_FILE",
    "ID42_WORK_WEB_TOKEN",
    "ID42_WORK_WEB_TOKEN_FILE",
    "ID42_WORK_ODOO_ENABLED",
    "ODOO_URL",
    "ODOO_DB",
    "ODOO_USER",
    "ODOO_API_KEY",
    "ODOO_API_KEY_FILE",
    "ODOO_PROTOCOL",
    "ODOO_TIMEOUT",
    "ID42_WORK_ODOO_READ_MODELS",
    "ID42_WORK_ODOO_WRITE_MODELS",
    "ID42_WORK_ODOO_READ_FIELDS",
    "ID42_WORK_GITHUB_ENABLED",
    "ID42_WORK_GITHUB_REPOSITORIES",
    "GITHUB_TOKEN",
    "ID42_WORK_GITHUB_TOKEN_FILE",
    "ID42_WORK_GITHUB_OPERATION_LOG",
)

# Matches id42_work.mcp.server.load_factory at the pinned id42-work commit.
_FACTORY_RE = re.compile(r"\A[A-Za-z_][\w.]*:[A-Za-z_]\w*\Z")
# Durable factory. id42_agent.mcp_stub:server is the empty in-memory factory.
_DEFAULT_WORK_FACTORY = "id42_agent.mcp_host:server"
_WORK_TOOL_TIMEOUT = 120
_IMAGE_PYTHONPATH = "/opt/id42-agent"
_AUTHORITY_ENV = "ID42_WORK_AUTHORITY_KEY_PATH"
_STORAGE_ENV = "ID42_WORK_STORAGE"
_DATABASE_ENV = "ID42_WORK_DATABASE_URL"
_DATABASE_FILE_ENV = "ID42_WORK_DATABASE_URL_FILE"
# Keep in sync with docker/id42_agent/materialize.py.
_DEFAULT_DATABASE_FILE = "/var/lib/id42-work/secrets/database.url"
_DEFAULT_ODOO_FILE = "/var/lib/id42-work/secrets/odoo.key"
_DEFAULT_GITHUB_FILE = "/var/lib/id42-work/secrets/github.token"
_WEB_TOKEN_REF = "${NANOBOT_WEB_TOKEN}"
_TELEGRAM_TOKEN_REF = "${TELEGRAM_BOT_TOKEN}"
_ENV_REF = re.compile(r"\A\$\{([A-Za-z_][A-Za-z0-9_]*)\}\Z")
# Keep in sync with docker/id42_agent/mcp_host.py DEFAULT_AUTHORITY_KEY.
_DEFAULT_AUTHORITY_KEY = "/var/lib/id42-work/authority.key"
# Keep in sync with docker/id42_agent/mcp_host.py storage_backend.
_STORAGE_SQLITE = "sqlite"
_STORAGE_POSTGRES = "postgres"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_MODEL_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_FIELD_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
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


def _authority_key_error(value: str) -> str | None:
    """Return an error when value is not an absolute key path, else None.

    An empty value means the factory default. The path is not secret.
    """
    if not value:
        return None
    if any(char.isspace() for char in value) or not value.startswith("/") or value.endswith("/"):
        return f"{_AUTHORITY_ENV} must be an absolute file path"
    parts = [part for part in value.split("/") if part]
    if not parts or ".." in parts:
        return f"{_AUTHORITY_ENV} must be an absolute file path"
    if parts[0] in {"home", "root"}:
        return f"{_AUTHORITY_ENV} must not live under /home or /root"
    return None


def _authority_key_path(raw: str) -> str:
    value = raw.strip()
    error = _authority_key_error(value)
    if error:
        raise ConfigError(error)
    return value


def _database_url_error(value: str) -> str | None:
    """Return an error when value is not a Postgres target, else None.

    Keep the rules in sync with docker/id42_agent/mcp_host.py. The message
    does not include the value.
    """
    if not value:
        return f"{_DATABASE_ENV} is required when {_STORAGE_ENV}=postgres"
    if any(char in value for char in "\r\n"):
        return f"{_DATABASE_ENV} must be a single line"
    if "://" in value:
        if any(char.isspace() for char in value):
            return f"{_DATABASE_ENV} must be a postgres URL or a libpq connection string"
        parsed = urlparse(value)
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            return f"{_DATABASE_ENV} must name a postgres host"
        return None
    hosts: list[str] = []
    for part in value.split():
        if "=" not in part:
            return f"{_DATABASE_ENV} must be a postgres URL or a libpq connection string"
        key, _, raw = part.partition("=")
        if key.lower() in {"host", "hostaddr"}:
            hosts.append(raw.strip("'\""))
    if not hosts:
        return f"{_DATABASE_ENV} must name a postgres host"
    for host in hosts:
        if host == "/home" or host.startswith("/home/") or host == "/root" or host.startswith("/root/"):
            return f"{_DATABASE_ENV} must not use a socket under /home or /root"
    return None


def _storage_choice(storage_raw: str, url_raw: str, file_raw: str) -> tuple[str, str]:
    """Return ``(sqlite|postgres, file path)``. The URL is never returned.

    SQLite drops both. Postgres writes a file path: the operator's
    ``ID42_WORK_DATABASE_URL_FILE``, or the path the entrypoint fills from
    an inline URL. A previous volume may still contain the URL itself.
    """
    storage = storage_raw.strip().lower()
    if storage in {"", _STORAGE_SQLITE}:
        return _STORAGE_SQLITE, ""
    if storage != _STORAGE_POSTGRES:
        raise ConfigError(f"{_STORAGE_ENV} must be sqlite or postgres")
    url = url_raw.strip()
    file_path = file_raw.strip()
    if url:
        error = _database_url_error(url)
        if error:
            raise ConfigError(error)
    if file_path:
        path_error = _secret_file_error(file_path, _DATABASE_FILE_ENV)
        if path_error:
            raise ConfigError(path_error)
    if not url and not file_path:
        raise ConfigError(
            f"{_DATABASE_ENV} or {_DATABASE_FILE_ENV} is required when {_STORAGE_ENV}=postgres"
        )
    return _STORAGE_POSTGRES, file_path or _DEFAULT_DATABASE_FILE


def _mcp_storage_errors(extra_env: Mapping[str, str]) -> list[str]:
    """Reject a storage env map the factory would not open. No secret text.

    A new render stores ``ID42_WORK_DATABASE_URL_FILE``. A volume rendered
    before that still stores ``ID42_WORK_DATABASE_URL``. Either is accepted.
    """
    storage = extra_env.get(_STORAGE_ENV, "").strip()
    url = extra_env.get(_DATABASE_ENV, "").strip()
    file_path = extra_env.get(_DATABASE_FILE_ENV, "").strip()
    if not storage and not url and not file_path:
        return []
    if storage.lower() in {"", _STORAGE_SQLITE}:
        if url or file_path:
            return [f"{_DATABASE_ENV} is set only when {_STORAGE_ENV}=postgres"]
        if storage and storage.lower() != _STORAGE_SQLITE:
            return [f"{_STORAGE_ENV} must be sqlite or postgres"]
        return []
    if storage.lower() != _STORAGE_POSTGRES:
        return [f"{_STORAGE_ENV} must be sqlite or postgres"]
    errors: list[str] = []
    if url:
        error = _database_url_error(url)
        if error:
            errors.append(error)
    if file_path:
        path_error = _secret_file_error(file_path, _DATABASE_FILE_ENV)
        if path_error:
            errors.append(path_error)
    if not url and not file_path:
        errors.append(
            f"{_DATABASE_ENV} or {_DATABASE_FILE_ENV} is required when {_STORAGE_ENV}=postgres"
        )
    return errors


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


def connector_errors(env: Mapping[str, str]) -> list[str]:
    """Reject incomplete connector settings. The messages do not include secrets."""
    errors: list[str] = []
    try:
        odoo = _parse_bool("ID42_WORK_ODOO_ENABLED", env.get("ID42_WORK_ODOO_ENABLED", "") or "false")
        github = _parse_bool(
            "ID42_WORK_GITHUB_ENABLED",
            env.get("ID42_WORK_GITHUB_ENABLED", "") or "false",
        )
    except ConfigError as exc:
        return [str(exc)]
    if odoo:
        errors.extend(_odoo_errors(env))
    if github:
        errors.extend(_github_errors(env))
    return errors


def _connector_server_env(env: Mapping[str, str]) -> dict[str, str]:
    """Env entries the stdio child needs when a connector is on."""
    copied: dict[str, str] = {}
    if _parse_bool("ID42_WORK_ODOO_ENABLED", env.get("ID42_WORK_ODOO_ENABLED", "") or "false"):
        copied["ID42_WORK_ODOO_ENABLED"] = "true"
        for name in (
            "ODOO_URL",
            "ODOO_DB",
            "ODOO_USER",
            "ODOO_PROTOCOL",
            "ODOO_TIMEOUT",
            "ID42_WORK_ODOO_READ_MODELS",
            "ID42_WORK_ODOO_WRITE_MODELS",
            "ID42_WORK_ODOO_READ_FIELDS",
        ):
            value = env.get(name, "").strip()
            if value:
                copied[name] = value
        key_file = env.get("ODOO_API_KEY_FILE", "").strip()
        if key_file:
            copied["ODOO_API_KEY_FILE"] = key_file
        elif env.get("ODOO_API_KEY", "").strip():
            copied["ODOO_API_KEY_FILE"] = _DEFAULT_ODOO_FILE
    if _parse_bool("ID42_WORK_GITHUB_ENABLED", env.get("ID42_WORK_GITHUB_ENABLED", "") or "false"):
        copied["ID42_WORK_GITHUB_ENABLED"] = "true"
        for name in (
            "ID42_WORK_GITHUB_REPOSITORIES",
            "ID42_WORK_GITHUB_OPERATION_LOG",
        ):
            value = env.get(name, "").strip()
            if value:
                copied[name] = value
        token_file = env.get("ID42_WORK_GITHUB_TOKEN_FILE", "").strip()
        if token_file:
            copied["ID42_WORK_GITHUB_TOKEN_FILE"] = token_file
        elif env.get("GITHUB_TOKEN", "").strip():
            copied["ID42_WORK_GITHUB_TOKEN_FILE"] = _DEFAULT_GITHUB_FILE
    return copied


def _odoo_errors(env: Mapping[str, str]) -> list[str]:
    errors: list[str] = []
    url = env.get("ODOO_URL", "").strip()
    if not url or any(char.isspace() for char in url):
        errors.append("ODOO_URL is required when Odoo is enabled")
    else:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            errors.append("ODOO_URL must be an http or https URL")
        elif parsed.username or parsed.password or parsed.query or parsed.fragment:
            errors.append("ODOO_URL must not embed credentials, a query, or a fragment")
    if not env.get("ODOO_DB", "").strip():
        errors.append("ODOO_DB is required when Odoo is enabled")
    if not env.get("ODOO_USER", "").strip():
        errors.append("ODOO_USER is required when Odoo is enabled")
    key = env.get("ODOO_API_KEY", "").strip()
    key_file = env.get("ODOO_API_KEY_FILE", "").strip()
    if key and _secret_error(key):
        errors.append("ODOO_API_KEY must be one line")
    if key_file:
        path_error = _secret_file_error(key_file, "ODOO_API_KEY_FILE")
        if path_error:
            errors.append(path_error)
    if not key and not key_file:
        errors.append("ODOO_API_KEY or ODOO_API_KEY_FILE is required when Odoo is enabled")
    protocol = env.get("ODOO_PROTOCOL", "").strip().lower()
    if protocol and protocol not in {"jsonrpc", "xmlrpc"}:
        errors.append("ODOO_PROTOCOL must be jsonrpc or xmlrpc")
    timeout = env.get("ODOO_TIMEOUT", "").strip()
    if timeout:
        try:
            number = float(timeout)
        except ValueError:
            number = 0
        if number < 1 or number > 300:
            errors.append("ODOO_TIMEOUT must be between 1 and 300 seconds")
    read_models = _name_list(
        env.get("ID42_WORK_ODOO_READ_MODELS", ""),
        "ID42_WORK_ODOO_READ_MODELS",
        _MODEL_RE,
        errors,
    )
    if not read_models and not any("ID42_WORK_ODOO_READ_MODELS" in item for item in errors):
        errors.append("ID42_WORK_ODOO_READ_MODELS is required when Odoo is enabled")
    write_models = _name_list(
        env.get("ID42_WORK_ODOO_WRITE_MODELS", ""),
        "ID42_WORK_ODOO_WRITE_MODELS",
        _MODEL_RE,
        errors,
    )
    if any(model not in read_models for model in write_models):
        errors.append("ID42_WORK_ODOO_WRITE_MODELS must also be listed in ID42_WORK_ODOO_READ_MODELS")
    _field_list_errors(env.get("ID42_WORK_ODOO_READ_FIELDS", ""), read_models, errors)
    return errors


def _github_errors(env: Mapping[str, str]) -> list[str]:
    errors: list[str] = []
    repos = _name_list(
        env.get("ID42_WORK_GITHUB_REPOSITORIES", ""),
        "ID42_WORK_GITHUB_REPOSITORIES",
        _REPO_RE,
        errors,
    )
    if not repos and not any("ID42_WORK_GITHUB_REPOSITORIES" in item for item in errors):
        errors.append("ID42_WORK_GITHUB_REPOSITORIES is required when GitHub is enabled")
    token = env.get("GITHUB_TOKEN", "").strip()
    token_file = env.get("ID42_WORK_GITHUB_TOKEN_FILE", "").strip()
    if token and token_file:
        errors.append("set GITHUB_TOKEN or ID42_WORK_GITHUB_TOKEN_FILE, not both")
    if token and _secret_error(token):
        errors.append("GITHUB_TOKEN must be one line")
    if token_file:
        path_error = _secret_file_error(token_file, "ID42_WORK_GITHUB_TOKEN_FILE")
        if path_error:
            errors.append(path_error)
    if not token and not token_file:
        errors.append("GITHUB_TOKEN or ID42_WORK_GITHUB_TOKEN_FILE is required when GitHub is enabled")
    log_path = env.get("ID42_WORK_GITHUB_OPERATION_LOG", "").strip()
    if log_path:
        path_error = _secret_file_error(log_path, "ID42_WORK_GITHUB_OPERATION_LOG")
        if path_error:
            errors.append(path_error)
    return errors


def _secret_error(value: str) -> bool:
    return any(char.isspace() for char in value) or len(value) > 4096


def _secret_file_error(value: str, label: str) -> str | None:
    path = Path(value)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        return f"{label} must be an absolute file path"
    for root in (Path("/home"), Path("/root")):
        if path == root or root in path.parents:
            return f"{label} must not live under /home or /root"
    return None


def _name_list(raw: str, label: str, pattern: re.Pattern[str], errors: list[str]) -> list[str]:
    if not raw.strip():
        return []
    items: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            errors.append(f"{label} has an empty entry")
            return items
        if pattern.fullmatch(item) is None:
            errors.append(f"{label} has an entry that is not allowed")
            return items
        if item in items:
            errors.append(f"{label} lists a name more than once")
            return items
        items.append(item)
    return items


def _field_list_errors(raw: str, read_models: list[str], errors: list[str]) -> None:
    text = raw.strip()
    if not text:
        return
    seen: set[str] = set()
    for part in text.split(";"):
        model, separator, fields = part.partition(":")
        model = model.strip()
        if not separator or not model or not fields.strip():
            errors.append("ID42_WORK_ODOO_READ_FIELDS must be model:field,field;model:field")
            return
        if model not in read_models:
            errors.append(
                "ID42_WORK_ODOO_READ_FIELDS names a model that is not in ID42_WORK_ODOO_READ_MODELS"
            )
            return
        if model in seen:
            errors.append("ID42_WORK_ODOO_READ_FIELDS lists a model more than once")
            return
        seen.add(model)
        names = _name_list(fields, "ID42_WORK_ODOO_READ_FIELDS", _FIELD_RE, errors)
        if "name" not in names:
            errors.append("ID42_WORK_ODOO_READ_FIELDS must include name for each listed model")
            return


def _work_server(env: dict[str, str]) -> dict[str, object] | None:
    """Stdio MCP server block, or None when ID42 Work is left off."""
    enabled = _parse_bool("ID42_WORK_ENABLED", env["ID42_WORK_ENABLED"] or "false")
    factory_raw = env["ID42_WORK_FACTORY"]
    pythonpath_raw = env["ID42_WORK_PYTHONPATH"]
    key_path = _authority_key_path(env.get(_AUTHORITY_ENV, ""))
    storage, database_file = _storage_choice(
        env.get(_STORAGE_ENV, ""),
        env.get(_DATABASE_ENV, ""),
        env.get(_DATABASE_FILE_ENV, ""),
    )
    if not enabled:
        if factory_raw.strip():
            _work_factory(factory_raw)
        if pythonpath_raw.strip():
            _work_pythonpath(pythonpath_raw)
        return None
    # nanobot's stdio client inherits PATH and HOME, not the gateway environment.
    # The default factory is imported from /opt/id42-agent. An unset key path
    # stays out of this map: the factory then uses /var/lib/id42-work/authority.key.
    # SQLite is the default, so those keys stay out of the map unless Postgres
    # is selected. The database URL is not written. The child receives a
    # mode 0600 file path and reads the URL from that file.
    server_env = {"PYTHONPATH": _work_pythonpath(pythonpath_raw) or _IMAGE_PYTHONPATH}
    if key_path:
        server_env[_AUTHORITY_ENV] = key_path
    if storage == _STORAGE_POSTGRES:
        server_env[_STORAGE_ENV] = _STORAGE_POSTGRES
        server_env[_DATABASE_FILE_ENV] = database_file
    server_env.update(_connector_server_env(env))
    server: dict[str, object] = {
        "command": "id42-work",
        "args": ["mcp", "serve", "--factory", _work_factory(factory_raw)],
        "toolTimeout": _WORK_TOOL_TIMEOUT,
        "env": server_env,
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
    if isinstance(extra_env, dict) and _AUTHORITY_ENV in extra_env:
        key_error = _authority_key_error(extra_env[_AUTHORITY_ENV].strip())
        if key_error:
            errors.append(f"{label}.env.{key_error}")
    if isinstance(extra_env, dict) and "ID42_WORK_WEB_TOKEN" in extra_env:
        errors.append(f"{label}.env must not carry ID42_WORK_WEB_TOKEN")
    if isinstance(extra_env, dict):
        string_env = {key: value for key, value in extra_env.items() if isinstance(value, str)}
        for item in _mcp_storage_errors(string_env):
            errors.append(f"{label}.env: {item}")
        for item in connector_errors(string_env):
            errors.append(f"{label}.env: {item}")
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
    connector_problems = connector_errors(picked)
    if connector_problems:
        raise ConfigError(connector_problems[0])

    telegram_enabled = _parse_bool("TELEGRAM_ENABLED", picked["TELEGRAM_ENABLED"] or "false")
    channels: dict[str, object] = {
        "websocket": {
            "enabled": True,
            "host": "0.0.0.0",
            "port": 8765,
            "tokenIssueSecret": _WEB_TOKEN_REF,
            "websocketRequiresToken": True,
        }
    }
    if telegram_enabled:
        token = picked["TELEGRAM_BOT_TOKEN"].strip()
        if not token or any(char.isspace() for char in token):
            raise ConfigError("TELEGRAM_BOT_TOKEN is required when TELEGRAM_ENABLED=true")
        channels["telegram"] = {
            "enabled": True,
            "token": _TELEGRAM_TOKEN_REF,
            "allowFrom": _allowlist(picked["TELEGRAM_ALLOWED_USERS"]),
            "mode": "polling",
        }

    tools: dict[str, object] = {
        "restrictToWorkspace": True,
        "webuiAllowRemotePackageInstall": False,
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
        expect(
            _dig(document, "tools", "webuiAllowRemotePackageInstall"),
            False,
            "tools.webuiAllowRemotePackageInstall",
        )
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


def _append_secret(found: list[str], value: object) -> None:
    if isinstance(value, str) and value.strip():
        found.append(value.strip())


def _secret_values(document: dict[str, object]) -> list[str]:
    found: list[str] = []
    channels = document.get("channels")
    if isinstance(channels, dict):
        websocket = channels.get("websocket")
        telegram = channels.get("telegram")
        for section, key in ((websocket, "tokenIssueSecret"), (telegram, "token")):
            if isinstance(section, dict):
                _append_secret(found, section.get(key))
    tools = document.get("tools")
    servers = tools.get("mcpServers") if isinstance(tools, dict) else None
    server = servers.get("id42-work") if isinstance(servers, dict) else None
    extra = server.get("env") if isinstance(server, dict) else None
    if isinstance(extra, dict):
        url = extra.get(_DATABASE_ENV)
        if isinstance(url, str) and url.strip() and _ENV_REF.fullmatch(url.strip()) is None:
            _append_secret(found, url)
            if "://" in url:
                password = urlparse(url).password
                _append_secret(found, password)
        for name in ("ODOO_API_KEY", "GITHUB_TOKEN", "ID42_WORK_WEB_TOKEN"):
            _append_secret(found, extra.get(name))
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
            "ID42_WORK_AUTHORITY_KEY_PATH": picked["ID42_WORK_AUTHORITY_KEY_PATH"].strip()
            or _DEFAULT_AUTHORITY_KEY,
            "ID42_WORK_STORAGE": (picked["ID42_WORK_STORAGE"].strip().lower() or _STORAGE_SQLITE),
            "id42_work_database_url_set": bool(picked["ID42_WORK_DATABASE_URL"].strip()),
            "id42_work_database_file_set": bool(picked["ID42_WORK_DATABASE_URL_FILE"].strip()),
            "id42_work_web_token_set": bool(picked["ID42_WORK_WEB_TOKEN"].strip()),
            "ID42_WORK_WEB_TOKEN_FILE": picked["ID42_WORK_WEB_TOKEN_FILE"].strip(),
            "ID42_WORK_ODOO_ENABLED": picked["ID42_WORK_ODOO_ENABLED"] or "false",
            "odoo_api_key_set": bool(picked["ODOO_API_KEY"].strip() or picked["ODOO_API_KEY_FILE"].strip()),
            "ID42_WORK_ODOO_READ_MODELS": picked["ID42_WORK_ODOO_READ_MODELS"].strip(),
            "ID42_WORK_ODOO_WRITE_MODELS": picked["ID42_WORK_ODOO_WRITE_MODELS"].strip(),
            "ID42_WORK_GITHUB_ENABLED": picked["ID42_WORK_GITHUB_ENABLED"] or "false",
            "github_token_set": bool(
                picked["GITHUB_TOKEN"].strip() or picked["ID42_WORK_GITHUB_TOKEN_FILE"].strip()
            ),
            "ID42_WORK_GITHUB_REPOSITORIES": picked["ID42_WORK_GITHUB_REPOSITORIES"].strip(),
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


def expand_env_ref(value: str) -> str:
    """Replace a whole-string ``${NAME}`` with the process environment."""
    match = _ENV_REF.fullmatch(value.strip())
    if match is None:
        return value
    return os.environ.get(match.group(1), "")


def telegram_getme_from_config(path: Path) -> None:
    document = _load_json(path)
    channels = document.get("channels")
    telegram = channels.get("telegram") if isinstance(channels, dict) else None
    token = telegram.get("token") if isinstance(telegram, dict) else ""
    if not isinstance(token, str):
        raise ConfigError("channels.telegram.token is missing")
    print(json.dumps(telegram_getme(expand_env_ref(token))))


def secret_warnings(env: Mapping[str, str]) -> list[str]:
    """Names and recommendations for inline secrets. Values are not included."""
    warnings: list[str] = []
    url = env.get("ID42_WORK_DATABASE_URL", "").strip()
    url_file = env.get("ID42_WORK_DATABASE_URL_FILE", "").strip()
    if url and url_file:
        warnings.append(
            "ID42_WORK_DATABASE_URL and ID42_WORK_DATABASE_URL_FILE are both set; "
            "the stdio child uses the file and operator scripts use the URL. "
            "Prefer ID42_WORK_DATABASE_URL_FILE alone"
        )
    elif url:
        warnings.append(
            "ID42_WORK_DATABASE_URL is inline; prefer ID42_WORK_DATABASE_URL_FILE "
            "(mode 0600). A new render stores the file path, and startup writes "
            "the URL into that file"
        )
    if env.get("ODOO_API_KEY", "").strip():
        warnings.append("ODOO_API_KEY is inline; prefer ODOO_API_KEY_FILE (mode 0600)")
    if env.get("GITHUB_TOKEN", "").strip():
        warnings.append(
            "GITHUB_TOKEN is inline; prefer ID42_WORK_GITHUB_TOKEN_FILE (mode 0600)"
        )
    if env.get("ID42_WORK_WEB_TOKEN", "").strip() and not env.get(
        "ID42_WORK_WEB_TOKEN_FILE", ""
    ).strip():
        warnings.append(
            "ID42_WORK_WEB_TOKEN is inline; prefer ID42_WORK_WEB_TOKEN_FILE (mode 0600)"
        )
    if env.get("NANOBOT_WEB_TOKEN", "").strip():
        warnings.append(
            "NANOBOT_WEB_TOKEN is inline; config.json stores ${NANOBOT_WEB_TOKEN}. "
            "This nanobot pin has no file field for tokenIssueSecret"
        )
    if env.get("TELEGRAM_BOT_TOKEN", "").strip():
        warnings.append(
            "TELEGRAM_BOT_TOKEN is inline; config.json stores ${TELEGRAM_BOT_TOKEN}. "
            "This nanobot pin has no file field for channels.telegram.token"
        )
    return warnings


def print_secret_warnings(path: Path) -> None:
    for line in secret_warnings(load_env_file(path)):
        print(line)


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

    warnings = sub.add_parser("secret-warnings")
    warnings.add_argument("--env-file", type=Path, required=True)

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
        elif args.command == "secret-warnings":
            print_secret_warnings(args.env_file)
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
