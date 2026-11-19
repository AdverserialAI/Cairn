from __future__ import annotations

import json
import os
import stat
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

BOOTSTRAP_ENV = "CAIRN_LLM_BOOTSTRAP_FILE"
RUNTIME_SECRET_ROOT = Path("/run/adverserial/current")
MODEL_PROXY_ORIGIN = "http://model-proxy:8089"

_MAX_BOOTSTRAP_BYTES = 64 * 1024
_MAX_TOKEN_BYTES = 4 * 1024
_MANAGED_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_MODEL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "CODEX_MODEL",
        "CODEX_BASE_URL",
        "OPENAI_API_KEY",
        "PI_MODEL",
        "PI_BASE_URL",
        "PI_API_KEY",
        "PI_PROVIDER_API",
    }
)
_PROVIDER_APIS = frozenset({"anthropic-messages", "openai-responses", "openai-completions"})


class ConfidentialBootstrapError(ValueError):
    """Raised when a managed LLM bootstrap document is unsafe or incomplete."""


def apply_confidential_bootstrap(data: Any) -> Any:
    """Overlay managed LLM settings without exposing a provider credential to Cairn."""

    bootstrap_path = os.environ.get(BOOTSTRAP_ENV, "").strip()
    if not bootstrap_path:
        return data
    if not isinstance(data, dict):
        raise ConfidentialBootstrapError("dispatch configuration must be an object")
    runtime = data.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("execution", "container") != "container":
        raise ConfidentialBootstrapError("confidential LLM bootstrap requires runtime.execution=container")

    document = _read_json_secret(Path(bootstrap_path), _MAX_BOOTSTRAP_BYTES)
    _require_exact_keys(document, {"version", "workers"}, "bootstrap")
    if document["version"] != 1:
        raise ConfidentialBootstrapError("bootstrap.version must be 1")
    entries = document["workers"]
    if not isinstance(entries, list) or not entries:
        raise ConfidentialBootstrapError("bootstrap.workers must be a non-empty array")

    configured_workers = data.get("workers")
    if not isinstance(configured_workers, list):
        raise ConfidentialBootstrapError("dispatch workers must be an array")
    by_name: dict[str, dict[str, Any]] = {}
    for worker in configured_workers:
        if not isinstance(worker, dict):
            raise ConfidentialBootstrapError("every dispatch worker must be an object")
        name = worker.get("name")
        if not isinstance(name, str) or not name:
            raise ConfidentialBootstrapError("every dispatch worker must have a name")
        if name in by_name:
            raise ConfidentialBootstrapError(f"duplicate dispatch worker: {name}")
        by_name[name] = worker

    result = deepcopy(data)
    common_env = result.get("common_env")
    if common_env is None:
        common_env = {}
    if not isinstance(common_env, dict):
        raise ConfidentialBootstrapError("common_env must be an object")
    result["common_env"] = {key: value for key, value in common_env.items() if key not in _MANAGED_ENV_KEYS}

    result_workers = result["workers"]
    result_by_name = {worker["name"]: worker for worker in result_workers}
    managed_names = {name for name, worker in by_name.items() if worker.get("type") != "mock"}
    seen: set[str] = set()

    for index, entry in enumerate(entries):
        context = f"bootstrap.workers[{index}]"
        if not isinstance(entry, dict):
            raise ConfidentialBootstrapError(f"{context} must be an object")
        allowed = {"name", "model", "transport", "base_url", "token_file", "provider_api"}
        required = {"name", "model", "transport", "base_url", "token_file"}
        unknown = set(entry) - allowed
        missing = required - set(entry)
        if unknown:
            raise ConfidentialBootstrapError(f"{context} has unknown fields: {', '.join(sorted(unknown))}")
        if missing:
            raise ConfidentialBootstrapError(f"{context} is missing fields: {', '.join(sorted(missing))}")

        name = _clean_text(entry["name"], f"{context}.name", 128)
        if name in seen:
            raise ConfidentialBootstrapError(f"duplicate bootstrap worker: {name}")
        seen.add(name)
        worker = result_by_name.get(name)
        if worker is None:
            raise ConfidentialBootstrapError(f"bootstrap references unknown worker: {name}")
        worker_type = worker.get("type")
        if worker_type == "mock":
            raise ConfidentialBootstrapError(f"mock worker {name} cannot use a provider bootstrap")
        if worker_type not in {"claudecode", "codex", "pi"}:
            raise ConfidentialBootstrapError(f"worker {name} has unsupported type: {worker_type}")
        if entry["transport"] != "agent-proxy":
            raise ConfidentialBootstrapError(f"{context}.transport must be agent-proxy")

        model = _clean_text(entry["model"], f"{context}.model", 256)
        base_url = _validate_proxy_url(entry["base_url"], f"{context}.base_url")
        token_path = _clean_text(entry["token_file"], f"{context}.token_file", 1024)
        token = _read_token(Path(token_path))

        env = worker.get("env")
        if env is None:
            env = {}
        if not isinstance(env, dict):
            raise ConfidentialBootstrapError(f"worker {name} env must be an object")
        clean_env = {key: value for key, value in env.items() if key not in _MANAGED_ENV_KEYS}

        if worker_type == "claudecode":
            if "provider_api" in entry:
                raise ConfidentialBootstrapError(f"{context}.provider_api is only valid for pi workers")
            clean_env.update(
                {
                    "ANTHROPIC_MODEL": model,
                    "ANTHROPIC_BASE_URL": base_url,
                    "ANTHROPIC_AUTH_TOKEN": token,
                }
            )
        elif worker_type == "codex":
            if "provider_api" in entry:
                raise ConfidentialBootstrapError(f"{context}.provider_api is only valid for pi workers")
            clean_env.update(
                {
                    "CODEX_MODEL": model,
                    "CODEX_BASE_URL": base_url,
                    "OPENAI_API_KEY": token,
                }
            )
        else:
            provider_api = entry.get("provider_api")
            if provider_api not in _PROVIDER_APIS:
                raise ConfidentialBootstrapError(
                    f"{context}.provider_api must be one of: " + ", ".join(sorted(_PROVIDER_APIS))
                )
            clean_env.update(
                {
                    "PI_MODEL": model,
                    "PI_BASE_URL": base_url,
                    "PI_API_KEY": token,
                    "PI_PROVIDER_API": provider_api,
                }
            )
        worker["env"] = clean_env

    missing_names = managed_names - seen
    if missing_names:
        raise ConfidentialBootstrapError("bootstrap is missing workers: " + ", ".join(sorted(missing_names)))
    return result


def _read_json_secret(path: Path, max_bytes: int) -> dict[str, Any]:
    raw = _read_secret(path, max_bytes)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfidentialBootstrapError(f"invalid bootstrap JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ConfidentialBootstrapError("bootstrap must be a JSON object")
    return value


def _read_token(path: Path) -> str:
    token = _read_secret(path, _MAX_TOKEN_BYTES).strip()
    if len(token) < 16 or len(token) > 4096:
        raise ConfidentialBootstrapError("proxy token length must be between 16 and 4096 bytes")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in token):
        raise ConfidentialBootstrapError("proxy token contains control characters")
    return token


def _read_secret(path: Path, max_bytes: int) -> str:
    root = RUNTIME_SECRET_ROOT.absolute()
    if not path.is_absolute():
        raise ConfidentialBootstrapError("runtime secret paths must be absolute")
    absolute = path.absolute()
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise ConfidentialBootstrapError(f"runtime secret path must be inside {root}") from exc
    if not relative.parts:
        raise ConfidentialBootstrapError("runtime secret path must name a file")

    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise ConfidentialBootstrapError(f"cannot inspect runtime secret root {root}: {exc}") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ConfidentialBootstrapError(f"runtime secret root must be a real directory: {root}")
    if stat.S_IMODE(root_metadata.st_mode) & 0o022:
        raise ConfidentialBootstrapError(f"runtime secret root is writable by another user: {root}")

    current = root
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ConfidentialBootstrapError(f"cannot inspect runtime secret {current}: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ConfidentialBootstrapError(f"runtime secret path cannot contain symlinks: {current}")
        if current != absolute and (not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o022):
            raise ConfidentialBootstrapError(f"runtime secret directory is not protected: {current}")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as secret_file:
            metadata = os.fstat(secret_file.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ConfidentialBootstrapError(f"runtime secret must be a regular file: {absolute}")
            if metadata.st_uid != os.geteuid():
                raise ConfidentialBootstrapError(f"runtime secret must be owned by the Cairn process: {absolute}")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise ConfidentialBootstrapError(f"runtime secret permissions are too broad: {absolute}")
            if metadata.st_size > max_bytes:
                raise ConfidentialBootstrapError(f"runtime secret is too large: {absolute}")
            return secret_file.read(max_bytes + 1)
    except ConfidentialBootstrapError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ConfidentialBootstrapError(f"cannot read runtime secret {absolute}: {exc}") from exc


def _validate_proxy_url(value: Any, context: str) -> str:
    url = _clean_text(value, context, 1024).rstrip("/")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ConfidentialBootstrapError(f"{context} is not a valid URL") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "model-proxy"
        or port != 8089
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfidentialBootstrapError(f"{context} must use the fixed {MODEL_PROXY_ORIGIN} origin")
    path_parts = [part for part in parsed.path.split("/") if part]
    if (
        any(part in {".", ".."} for part in path_parts)
        or "//" in parsed.path
        or "%" in parsed.path
        or "\\" in parsed.path
    ):
        raise ConfidentialBootstrapError(f"{context} contains an unsafe path")
    return url


def _clean_text(value: Any, context: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ConfidentialBootstrapError(f"{context} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > max_length:
        raise ConfidentialBootstrapError(f"{context} has an invalid length")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in cleaned):
        raise ConfidentialBootstrapError(f"{context} contains control characters")
    return cleaned


def _require_exact_keys(value: dict[str, Any], keys: set[str], context: str) -> None:
    unknown = set(value) - keys
    missing = keys - set(value)
    if unknown:
        raise ConfidentialBootstrapError(f"{context} has unknown fields: {', '.join(sorted(unknown))}")
    if missing:
        raise ConfidentialBootstrapError(f"{context} is missing fields: {', '.join(sorted(missing))}")
