from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from cairn.dispatcher import confidential
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.confidential import ConfidentialBootstrapError


def _dispatch_payload() -> dict:
    return {
        "server": "http://cairn-server:8000",
        "runtime": {
            "interval": 3,
            "max_workers": 3,
            "max_running_projects": 1,
            "max_project_workers": 3,
            "healthcheck_timeout": 20,
            "prompt_group": "default",
            "execution": "container",
        },
        "tasks": {
            "bootstrap": {"timeout": 30, "conclude_timeout": 10},
            "reason": {"timeout": 30, "max_intents": 2},
            "explore": {"timeout": 30, "conclude_timeout": 10},
        },
        "container": {
            "image": "cairn-worker:test",
            "network_mode": "host",
            "completed_action": "stop",
        },
        "common_env": {"OPENAI_API_KEY": "must-be-removed", "SHARED": "kept"},
        "workers": [
            {
                "name": "claude",
                "type": "claudecode",
                "task_types": ["bootstrap", "reason", "explore"],
                "max_running": 1,
                "priority": 0,
                "env": {
                    "ANTHROPIC_MODEL": "unsafe-model",
                    "ANTHROPIC_BASE_URL": "https://provider.invalid",
                    "ANTHROPIC_AUTH_TOKEN": "provider-secret",
                },
            },
            {
                "name": "codex",
                "type": "codex",
                "task_types": ["reason"],
                "max_running": 1,
                "priority": 1,
                "env": {},
            },
            {
                "name": "pi",
                "type": "pi",
                "task_types": ["explore"],
                "max_running": 1,
                "priority": 2,
                "env": {"PI_MODEL_CONTEXT_WINDOW": "131072"},
            },
        ],
    }


def _write_managed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, document: dict | None = None
) -> tuple[Path, dict]:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    tokens: dict[str, Path] = {}
    for name in ("claude", "codex", "pi"):
        token_path = runtime_root / f"{name}.token"
        token_path.write_text(f"local-capability-{name}-0123456789\n", encoding="utf-8")
        token_path.chmod(0o600)
        tokens[name] = token_path
    if document is None:
        document = {
            "version": 1,
            "workers": [
                {
                    "name": "claude",
                    "model": "claude-managed",
                    "transport": "agent-proxy",
                    "base_url": "http://model-proxy:8089/anthropic",
                    "token_file": str(tokens["claude"]),
                },
                {
                    "name": "codex",
                    "model": "gpt-managed",
                    "transport": "agent-proxy",
                    "base_url": "http://model-proxy:8089/v1",
                    "token_file": str(tokens["codex"]),
                },
                {
                    "name": "pi",
                    "model": "pi-managed",
                    "transport": "agent-proxy",
                    "base_url": "http://model-proxy:8089/v1",
                    "token_file": str(tokens["pi"]),
                    "provider_api": "openai-responses",
                },
            ],
        }
    bootstrap_path = runtime_root / "bootstrap.json"
    bootstrap_path.write_text(json.dumps(document), encoding="utf-8")
    bootstrap_path.chmod(0o600)
    monkeypatch.setattr(confidential, "RUNTIME_SECRET_ROOT", runtime_root)
    monkeypatch.setenv(confidential.BOOTSTRAP_ENV, str(bootstrap_path))
    return bootstrap_path, document


def _write_dispatch(tmp_path: Path, payload: dict | None = None) -> Path:
    path = tmp_path / "dispatch.yaml"
    path.write_text(yaml.safe_dump(payload or _dispatch_payload()), encoding="utf-8")
    return path


def test_confidential_bootstrap_replaces_provider_credentials_with_local_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_managed_files(tmp_path, monkeypatch)

    config = DispatchConfig.load(_write_dispatch(tmp_path))
    workers = {worker.name: worker for worker in config.workers}

    assert workers["claude"].env["ANTHROPIC_BASE_URL"] == "http://model-proxy:8089/anthropic"
    assert workers["claude"].env["ANTHROPIC_AUTH_TOKEN"].startswith("local-capability-")
    assert workers["codex"].env["CODEX_BASE_URL"] == "http://model-proxy:8089/v1"
    assert workers["codex"].env["OPENAI_API_KEY"].startswith("local-capability-")
    assert workers["pi"].env["PI_PROVIDER_API"] == "openai-responses"
    assert workers["pi"].env["PI_MODEL_CONTEXT_WINDOW"] == "131072"
    serialized = json.dumps(config.model_dump())
    assert "provider-secret" not in serialized
    assert "must-be-removed" not in serialized
    assert "https://provider.invalid" not in serialized


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda document: document.update({"unexpected": True}), "unknown fields"),
        (
            lambda document: document["workers"][0].update({"transport": "direct"}),
            "transport must be agent-proxy",
        ),
        (
            lambda document: document["workers"][0].update(
                {"base_url": "https://api.anthropic.com"}
            ),
            "fixed http://model-proxy:8089 origin",
        ),
        (lambda document: document["workers"].pop(), "missing workers: pi"),
        (
            lambda document: document["workers"][0].update({"provider_api": "x"}),
            "only valid for pi workers",
        ),
    ],
)
def test_confidential_bootstrap_fails_closed_on_invalid_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate,
    message: str,
) -> None:
    _, document = _write_managed_files(tmp_path, monkeypatch)
    mutate(document)
    bootstrap_path = Path(os.environ[confidential.BOOTSTRAP_ENV])
    bootstrap_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ConfidentialBootstrapError, match=message):
        DispatchConfig.load(_write_dispatch(tmp_path))


def test_confidential_bootstrap_rejects_broad_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap_path, _ = _write_managed_files(tmp_path, monkeypatch)
    bootstrap_path.chmod(0o644)

    with pytest.raises(ConfidentialBootstrapError, match="permissions are too broad"):
        DispatchConfig.load(_write_dispatch(tmp_path))


def test_confidential_bootstrap_rejects_symlinked_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, document = _write_managed_files(tmp_path, monkeypatch)
    target = tmp_path / "runtime" / "target.token"
    target.write_text("local-capability-target-0123456789", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "runtime" / "linked.token"
    link.symlink_to(target)
    document["workers"][0]["token_file"] = str(link)
    bootstrap_path = Path(os.environ[confidential.BOOTSTRAP_ENV])
    bootstrap_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ConfidentialBootstrapError, match="cannot contain symlinks"):
        DispatchConfig.load(_write_dispatch(tmp_path))


def test_confidential_bootstrap_rejects_local_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_managed_files(tmp_path, monkeypatch)
    payload = _dispatch_payload()
    payload["runtime"]["execution"] = "local"
    payload["local"] = {"completed_action": "keep"}

    with pytest.raises(ConfidentialBootstrapError, match="requires runtime.execution=container"):
        DispatchConfig.load(_write_dispatch(tmp_path, payload))
