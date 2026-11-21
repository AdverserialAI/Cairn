from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from cairn.dispatcher import confidential
from cairn.dispatcher.confidential import ConfidentialBootstrapError
from cairn.dispatcher.config import DispatchConfig


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
            lambda document: document["workers"][0].update({"base_url": "https://api.anthropic.com"}),
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


def test_confidential_bootstrap_rejects_broad_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap_path, _ = _write_managed_files(tmp_path, monkeypatch)
    bootstrap_path.chmod(0o644)

    with pytest.raises(ConfidentialBootstrapError, match="permissions are too broad"):
        DispatchConfig.load(_write_dispatch(tmp_path))


def test_confidential_bootstrap_rejects_symlinked_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_confidential_bootstrap_rejects_local_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_managed_files(tmp_path, monkeypatch)
    payload = _dispatch_payload()
    payload["runtime"]["execution"] = "local"
    payload["local"] = {"completed_action": "keep"}

    with pytest.raises(ConfidentialBootstrapError, match="requires runtime.execution=container"):
        DispatchConfig.load(_write_dispatch(tmp_path, payload))


# ---- the hosting platform's own document: providers, not workers --------------------


def _platform_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, providers: list[dict]) -> Path:
    path, _ = _write_managed_files(tmp_path, monkeypatch, document={"version": 1, "providers": providers})
    return path


def _provider(tmp_path: Path, provider: str, model: str, path: str = "") -> dict:
    return {
        "provider": provider,
        "model": model,
        "transport": "agent-proxy",
        "base_url": "http://model-proxy:8089" + path,
        "api_key_file": str(tmp_path / "runtime" / "claude.token"),
    }


@pytest.mark.parametrize(
    ("provider", "path", "worker", "expected"),
    [
        ("anthropic", "", "claude", {"ANTHROPIC_MODEL": "m", "ANTHROPIC_BASE_URL": "http://model-proxy:8089"}),
        ("openai-responses", "/v1", "codex", {"CODEX_MODEL": "m", "CODEX_BASE_URL": "http://model-proxy:8089/v1"}),
        (
            "openai",
            "/v1",
            "pi",
            {"PI_MODEL": "m", "PI_BASE_URL": "http://model-proxy:8089/v1", "PI_PROVIDER_API": "openai-completions"},
        ),
    ],
)
def test_platform_provider_runs_only_the_worker_that_speaks_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, path: str, worker: str, expected: dict
) -> None:
    _platform_document(tmp_path, monkeypatch, [_provider(tmp_path, provider, "m", path)])

    config = DispatchConfig.load(_write_dispatch(tmp_path))

    assert [w.name for w in config.workers] == [worker]
    env = config.workers[0].env
    for key, value in expected.items():
        assert env[key] == value
    serialized = json.dumps(config.model_dump())
    assert "provider-secret" not in serialized and "must-be-removed" not in serialized
    assert "local-capability-claude" in serialized  # the local token, never a provider key
    assert env["SHARED"] == "kept"


def test_two_platform_providers_run_two_kinds_of_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _platform_document(
        tmp_path,
        monkeypatch,
        [_provider(tmp_path, "anthropic", "claude-x"), _provider(tmp_path, "openai-responses", "gpt-x", "/v1")],
    )
    config = DispatchConfig.load(_write_dispatch(tmp_path))
    assert sorted(w.name for w in config.workers) == ["claude", "codex"]


def test_platform_document_with_no_provider_means_waiting_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _platform_document(tmp_path, monkeypatch, [])
    assert confidential.managed() is True
    assert confidential.connected_models() == []


def test_connected_models_never_include_a_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _platform_document(tmp_path, monkeypatch, [_provider(tmp_path, "openai", "adverserial/cyberseek", "/v1")])
    assert confidential.connected_models() == [{"provider": "openai", "model": "adverserial/cyberseek"}]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"provider": "gemini"}, "not supported"),
        ({"transport": "direct"}, "must be agent-proxy"),
        ({"base_url": "https://api.openai.com/v1"}, "fixed http://model-proxy:8089 origin"),
        ({"base_url": "http://model-proxy:8089/../x"}, "unsafe path"),
        ({"api_key_file": "/etc/passwd"}, "must be inside"),
        ({"extra": "field"}, "unknown fields"),
    ],
)
def test_platform_document_is_validated_strictly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict, message: str
) -> None:
    _platform_document(tmp_path, monkeypatch, [{**_provider(tmp_path, "openai", "m", "/v1"), **change}])
    with pytest.raises(ConfidentialBootstrapError, match=message):
        DispatchConfig.load(_write_dispatch(tmp_path))


def test_platform_document_refuses_duplicates_and_more_than_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    one = _provider(tmp_path, "openai", "m", "/v1")
    path = _platform_document(tmp_path, monkeypatch, [one, one])
    with pytest.raises(ConfidentialBootstrapError, match="duplicate bootstrap provider"):
        confidential.connected_models()
    three = [one, _provider(tmp_path, "anthropic", "m"), _provider(tmp_path, "openai-responses", "m", "/v1")]
    path.write_text(json.dumps({"version": 1, "providers": three}))
    with pytest.raises(ConfidentialBootstrapError, match="at most 2"):
        confidential.connected_models()


def test_fingerprint_changes_when_the_model_or_its_token_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _platform_document(tmp_path, monkeypatch, [])
    idle = confidential.bootstrap_fingerprint()
    assert idle == confidential.bootstrap_fingerprint()
    path.write_text(json.dumps({"version": 1, "providers": [_provider(tmp_path, "openai", "a", "/v1")]}))
    connected = confidential.bootstrap_fingerprint()
    assert connected != idle
    path.write_text(json.dumps({"version": 1, "providers": [_provider(tmp_path, "openai", "b", "/v1")]}))
    assert confidential.bootstrap_fingerprint() != connected
    another_model = confidential.bootstrap_fingerprint()
    token = tmp_path / "runtime" / "claude.token"
    token.write_text("local-capability-rotated-0123456789\n")
    token.chmod(0o600)
    assert confidential.bootstrap_fingerprint() != another_model


def test_local_workers_need_the_deployment_to_say_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _platform_document(tmp_path, monkeypatch, [_provider(tmp_path, "anthropic", "m")])
    payload = _dispatch_payload()
    payload["runtime"]["execution"] = "local"
    payload["local"] = {"completed_action": "keep"}
    payload.pop("container")
    with pytest.raises(ConfidentialBootstrapError, match=r"requires runtime\.execution=container"):
        DispatchConfig.load(_write_dispatch(tmp_path, payload))
    monkeypatch.setenv(confidential.LOCAL_WORKERS_ENV, "1")
    config = DispatchConfig.load(_write_dispatch(tmp_path, payload))
    assert config.runtime.execution == "local" and [w.name for w in config.workers] == ["claude"]
    assert config.workers[0].env["ANTHROPIC_AUTH_TOKEN"].startswith("local-capability-")
