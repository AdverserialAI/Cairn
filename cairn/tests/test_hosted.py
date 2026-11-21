"""A hosted, single-owner deployment: the front door signs the owner in, and Cairn shows its evidence."""

from __future__ import annotations

import importlib
import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from cairn.dispatcher import confidential
from cairn.server import app as app_module
from cairn.server import attestation, db, hosted
from cairn.server.auth import configure_api_keys

_KEY = "test-hosted-key"


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    configure_api_keys([_KEY])
    return TestClient(app_module.app, **kwargs)


@pytest.fixture
def front_door_app(monkeypatch: pytest.MonkeyPatch):
    """The application as it is built when the deployment says a front door signs the owner in."""

    monkeypatch.setenv(hosted.FRONT_DOOR_ENV, "1")
    importlib.reload(app_module)
    yield app_module.app
    monkeypatch.delenv(hosted.FRONT_DOOR_ENV)
    importlib.reload(app_module)


def test_without_a_front_door_nothing_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(hosted.FRONT_DOOR_ENV, raising=False)
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/projects").status_code == 401
        assert client.get("/projects", headers={"X-API-Key": _KEY}).status_code == 200
        assert client.post("/auth/register", json={"username": "alice", "password": "secret123"}).status_code == 200
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/healthz").json()["service"] == "cairn"


@pytest.mark.parametrize("value", ["", "0", "true", "yes", " 1x"])
def test_only_the_exact_setting_turns_accounts_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(hosted.FRONT_DOOR_ENV, value)
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/projects").status_code == 401


def test_behind_a_front_door_the_owner_needs_no_cairn_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, front_door_app
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/projects").status_code == 200
        assert client.get("/settings").status_code == 200
        # nobody can create or use a Cairn account, and no API description is published
        assert client.post("/auth/register", json={"username": "mallory", "password": "secret123"}).status_code == 404
        assert client.post("/auth/login", json={"username": "mallory", "password": "secret123"}).status_code == 404
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404
        # and no other site is invited to read its answers
        answer = client.get("/projects", headers={"Origin": "https://elsewhere.example"})
        assert "access-control-allow-origin" not in answer.headers


# ---- the connected model -------------------------------------------------------------


def test_model_endpoint_reports_what_is_connected_and_never_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch, headers={"X-API-Key": _KEY}) as client:
        monkeypatch.delenv(confidential.BOOTSTRAP_ENV, raising=False)
        assert client.get("/model").json() == {"managed": False}

        root = tmp_path / "runtime"
        root.mkdir(mode=0o700)
        token = root / "token"
        token.write_text("local-capability-0123456789abcdef\n")
        token.chmod(0o600)
        document = root / "bootstrap.json"
        document.write_text(json.dumps({"version": 1, "providers": []}))
        document.chmod(0o600)
        monkeypatch.setattr(confidential, "RUNTIME_SECRET_ROOT", root)
        monkeypatch.setenv(confidential.BOOTSTRAP_ENV, str(document))
        assert client.get("/model").json() == {"managed": True, "ready": False, "models": []}

        document.write_text(
            json.dumps(
                {
                    "version": 1,
                    "providers": [
                        {
                            "provider": "openai",
                            "model": "adverserial/cyberseek",
                            "transport": "agent-proxy",
                            "base_url": "http://model-proxy:8089/v1",
                            "api_key_file": str(token),
                        }
                    ],
                }
            )
        )
        answer = client.get("/model")
        assert answer.json() == {
            "managed": True,
            "ready": True,
            "models": [{"provider": "openai", "model": "adverserial/cyberseek"}],
        }
        assert "local-capability" not in answer.text and "model-proxy" not in answer.text

        document.write_text("not json")
        assert client.get("/model").json() == {"managed": True, "ready": False, "models": []}


# ---- hardware evidence ---------------------------------------------------------------


class _GuestAgent(BaseHTTPRequestHandler):
    vendor = "Google"
    calls: ClassVar[list[tuple[str, dict]]] = []

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        method = self.path.strip("/")
        type(self).calls.append((method, body))
        answers = {
            "GetQuote": {"quote": "aa", "event_log": "[]", "report_data": body.get("report_data", "")},
            "Info": {"cloud_vendor": type(self).vendor, "tcb_info": "{}"},
            "Attest": {"attestation": "signed-evidence"},
        }
        raw = json.dumps(answers.get(method, {"error": "unknown"})).encode()
        self.send_response(200 if method in answers else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args) -> None:
        return None

    def address_string(self) -> str:
        return "guest"


@pytest.fixture
def guest_agent(monkeypatch: pytest.MonkeyPatch):
    import tempfile

    directory = tempfile.mkdtemp(prefix="cg", dir="/tmp")
    path = f"{directory}/d.sock"
    _GuestAgent.calls = []
    _GuestAgent.vendor = "Google"
    server = socketserver.UnixStreamServer(path, _GuestAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv(attestation.DSTACK_SOCKET_ENV, path)
    yield _GuestAgent
    server.shutdown()
    server.server_close()
    Path(path).unlink(missing_ok=True)
    Path(directory).rmdir()


def test_evidence_is_bound_to_the_browsers_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guest_agent) -> None:
    nonce = "ab" * 32
    with _client(tmp_path, monkeypatch) as client:
        answer = client.get("/attestation", params={"reportData": nonce})
        assert answer.status_code == 200 and answer.headers["cache-control"] == "no-store"
        body = answer.json()
        assert body["report_data"] == nonce and body["attestation"] == "signed-evidence"
        assert ("GetQuote", {"report_data": nonce}) in guest_agent.calls
        assert ("Attest", {"report_data": nonce}) in guest_agent.calls
        assert client.get("/info").json()["cloud_vendor"] == "Google"
        # evidence is public by design: it needs no account, so anyone can check the machine
        assert client.get("/projects").status_code == 401


def test_other_hosts_get_the_plain_quote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guest_agent) -> None:
    guest_agent.vendor = "Other"
    with _client(tmp_path, monkeypatch) as client:
        body = client.get("/attestation", params={"reportData": "0x" + "cd" * 8}).json()
    assert "attestation" not in body and body["report_data"] == "cd" * 8
    assert not any(method == "Attest" for method, _ in guest_agent.calls)


@pytest.mark.parametrize("value", ["zz", "abc", "ab" * 65])
def test_malformed_values_are_refused_before_the_machine_is_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guest_agent, value: str
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/attestation", params={"reportData": value}).status_code in (400, 422)
    assert guest_agent.calls == []


def test_outside_a_confidential_machine_it_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(attestation.DSTACK_SOCKET_ENV, str(tmp_path / "missing.sock"))
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/attestation").status_code == 502
        assert client.get("/info").status_code == 502


def test_only_read_only_guest_agent_calls_exist() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        attestation.guest_agent("GetKey", {})


def test_the_default_verifier_comes_from_the_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(hosted.VERIFIER_URL_ENV, "https://attest.example/verify")
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/attestation/config").json() == {"verifier_url": "https://attest.example/verify"}


def test_the_page_offers_attestation_and_the_connected_model() -> None:
    page = (Path(app_module.__file__).parent / "static" / "index.html").read_text()
    assert page.count('@click="openAttestation()"') == 2
    for needed in ("verifyAttestation()", "/attestation?reportData=", "compose-hash", "loadModelState()"):
        assert needed in page


# ---- the hosted deployment's own files -----------------------------------------------

_REPO = Path(__file__).resolve().parents[2]


def test_hosted_dispatch_file_runs_the_worker_for_the_connected_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.dispatcher.config import DispatchConfig

    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    token = root / "token"
    token.write_text("local-capability-0123456789abcdef\n")
    token.chmod(0o600)
    document = root / "bootstrap.json"
    document.chmod(0o600) if document.exists() else None
    document.write_text(
        json.dumps(
            {
                "version": 1,
                "providers": [
                    {
                        "provider": "openai",
                        "model": "adverserial/cyberseek",
                        "transport": "agent-proxy",
                        "base_url": "http://model-proxy:8089/v1",
                        "api_key_file": str(token),
                    }
                ],
            }
        )
    )
    document.chmod(0o600)
    monkeypatch.setattr(confidential, "RUNTIME_SECRET_ROOT", root)
    monkeypatch.setenv(confidential.BOOTSTRAP_ENV, str(document))
    monkeypatch.setenv(confidential.LOCAL_WORKERS_ENV, "1")

    config = DispatchConfig.load(_REPO / "hosted" / "dispatch.hosted.yaml")

    assert config.runtime.execution == "local" and config.container is None
    assert config.server == "http://cairn-server:8000" and config.server_token == ""
    assert [worker.name for worker in config.workers] == ["pi-primary"]
    assert config.workers[0].env["PI_MODEL"] == "adverserial/cyberseek"
    text = (_REPO / "hosted" / "dispatch.hosted.yaml").read_text()
    for forbidden in ("API_KEY", "AUTH_TOKEN", "TSEC_", "docker.sock"):
        assert forbidden not in text


def test_hosted_image_runs_unprivileged_and_never_uses_the_docker_socket() -> None:
    recipe = (_REPO / "Dockerfile.hosted").read_text()
    assert "USER 65532:65532" in recipe.rsplit("ENTRYPOINT", 1)[0].rsplit("RUN ", 1)[1]
    assert "/NOPASSWD/d" in recipe and "docker.sock" not in recipe
    entry = (_REPO / "hosted" / "cairn-hosted").read_text()
    assert "sudo" not in entry and "docker" not in entry
    instructions = (_REPO / "hosted" / "AGENTS.md").read_text()
    assert "CTF" not in instructions and "TSEC" not in instructions


def test_a_new_local_project_starts_from_the_deployments_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.dispatcher.config import LocalConfig
    from cairn.dispatcher.runtime import local_backend

    template = tmp_path / "template"
    template.mkdir()
    (template / "AGENTS.md").write_text("rules")
    monkeypatch.setenv(local_backend.WORKSPACE_TEMPLATE_ENV, str(template))
    backend = local_backend.LocalBackend(LocalConfig(workspace_root=str(tmp_path / "work")))

    project = Path(backend.ensure_running("p1"))
    assert (project / "AGENTS.md").read_text() == "rules"
    # an existing project is never overwritten
    (project / "AGENTS.md").write_text("edited by the owner")
    backend.ensure_running("p1")
    assert (project / "AGENTS.md").read_text() == "edited by the owner"

    monkeypatch.delenv(local_backend.WORKSPACE_TEMPLATE_ENV)
    assert list(Path(backend.ensure_running("p2")).iterdir()) == []
