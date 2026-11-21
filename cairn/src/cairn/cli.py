import logging
import os
import threading
import time
from pathlib import Path

import click
import uvicorn

from cairn.dispatcher import confidential
from cairn.dispatcher.logging import configure_logging
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server import db

LOG = logging.getLogger(__name__)
MODEL_POLL_SECONDS = 5
# Asks whatever supervises the dispatcher (the deployment's restart policy) to start it again.
MODEL_CHANGED_EXIT = 75


def _wait_for_connected_model(wait: bool) -> None:
    """In a hosted deployment the owner connects the model after launch; idle until then."""

    announced = False
    while True:
        try:
            models = confidential.connected_models()
        except confidential.ConfidentialBootstrapError as exc:
            if not wait:
                raise click.ClickException(str(exc)) from exc
            models = []
            if not announced:
                LOG.warning("model configuration is not usable yet (%s); waiting", exc)
        if models is None or models:
            return
        if not wait:
            raise click.ClickException("no model is connected to this deployment yet")
        if not announced:
            LOG.info("no model is connected yet; the dispatcher is idle until the owner connects one")
            announced = True
        time.sleep(MODEL_POLL_SECONDS)


def _restart_when_model_changes() -> None:
    """The model settings are read once at start, so a change means starting again."""

    started_with = confidential.bootstrap_fingerprint()

    def watch() -> None:
        while True:
            time.sleep(MODEL_POLL_SECONDS)
            if confidential.bootstrap_fingerprint() != started_with:
                LOG.info("the connected model changed; restarting the dispatcher to use it")
                logging.shutdown()
                os._exit(MODEL_CHANGED_EXIT)

    threading.Thread(target=watch, name="model-watch", daemon=True).start()


@click.group()
def main():
    """Cairn - Fact-graph based collaborative exploration protocol."""


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host")
@click.option("--port", default=8000, show_default=True, help="Bind port")
@click.option(
    "--db-path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.option("--log-level", default="info", show_default=True, help="Uvicorn log level")
@click.option("--access-log/--no-access-log", default=True, show_default=True, help="Enable Uvicorn access log")
def serve(host: str, port: int, db_path: str, log_level: str, access_log: bool):
    """Start the Cairn API server."""
    db.configure(Path(db_path))
    from cairn.server.app import app

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=access_log,
    )


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--once", is_flag=True, help="Run one scheduling iteration and exit")
@click.option(
    "--startup-healthcheck-only",
    is_flag=True,
    help="Run startup worker healthchecks and exit",
)
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
def dispatch(config_path: Path, once: bool, startup_healthcheck_only: bool, log_level: str):
    """Run the Cairn dispatcher."""
    configure_logging(log_level, bare=startup_healthcheck_only)
    if confidential.managed():
        _wait_for_connected_model(wait=not (once or startup_healthcheck_only))
    loop = DispatcherLoop(config_path)
    if confidential.managed() and not (once or startup_healthcheck_only):
        _restart_when_model_changes()
    try:
        if startup_healthcheck_only:
            loop.run_startup_healthchecks_only()
            return
        loop.run(once=once)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
