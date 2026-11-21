from __future__ import annotations

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, RegexSessionDriver
from cairn.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env


_MODEL_KEYS = ("CODEX_MODEL", "CODEX_BASE_URL", "OPENAI_API_KEY")


class CodexDriver(RegexSessionDriver):
    type_name = "codex"

    def __init__(self, local: bool = False):
        self.local = local

    def local_binary(self) -> str | None:
        return "codex"

    def _native(self, worker: WorkerConfig) -> bool:
        """Whether to run codex with the host's own configuration.

        That is what local execution means on a developer's machine, where codex is already
        logged in. A deployment that connects the model outside Cairn also runs workers
        locally, but there the worker carries the model settings, and codex has no
        configuration of its own to fall back on: it must be given them.
        """
        return self.local and not all(worker.env.get(key) for key in _MODEL_KEYS)

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        env = worker.env
        return http_ping(
            f"{env['CODEX_BASE_URL']}/responses",
            headers={
                "Authorization": f"Bearer {env['OPENAI_API_KEY']}",
                "content-type": "application/json",
            },
            json_body={
                "model": env["CODEX_MODEL"],
                "input": [{"role": "user", "content": "ping"}],
                "stream": False,
            },
            timeout=timeout,
            proxies=proxies_from_env(env),
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        return f"POST {worker.env['CODEX_BASE_URL']}/responses (model={worker.env['CODEX_MODEL']})"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        if self._native(worker):
            return DriverResult(
                argv=[
                    "codex",
                    "exec",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--",
                    prompt,
                ]
            )
        env = worker.env
        return DriverResult(
            argv=[
                "codex",
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "--model",
                env["CODEX_MODEL"],
                "-c",
                'model_provider="cairn"',
                "-c",
                'model_providers.cairn.name="cairn"',
                "-c",
                'model_providers.cairn.wire_api="responses"',
                "-c",
                'model_reasoning_effort="high"',
                "-c",
                f'model_providers.cairn.base_url="{env["CODEX_BASE_URL"]}"',
                "-c",
                'model_providers.cairn.env_key="OPENAI_API_KEY"',
                "--",
                prompt,
            ]
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        if self._native(worker):
            return [
                "codex",
                "exec",
                "resume",
                session,
                "--dangerously-bypass-approvals-and-sandbox",
                "--",
                prompt,
            ]
        env = worker.env
        return [
            "codex",
            "exec",
            "resume",
            session,
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            env["CODEX_MODEL"],
            "-c",
            'model_provider="cairn"',
            "-c",
            'model_providers.cairn.name="cairn"',
            "-c",
            'model_providers.cairn.wire_api="responses"',
            "-c",
            'model_reasoning_effort="high"',
            "-c",
            f'model_providers.cairn.base_url="{env["CODEX_BASE_URL"]}"',
            "-c",
            'model_providers.cairn.env_key="OPENAI_API_KEY"',
            "--",
            prompt,
        ]
