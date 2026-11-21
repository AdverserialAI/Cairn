import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cairn import __version__
from cairn.dispatcher import confidential
from cairn.server import attestation, db, hosted
from cairn.server.auth import _api_keys_configured, configure_api_keys
from cairn.server.routers import auth, export, hints, intents, projects, settings

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.configure(db.DEFAULT_DB)
    if not _api_keys_configured():
        api_keys = [k.strip() for k in os.environ.get("CAIRN_API_KEYS", "").split(",") if k.strip()]
        configure_api_keys(api_keys)
    yield


app = FastAPI(
    title="Cairn",
    description="Fact-graph based collaborative exploration protocol",
    version=__version__,
    lifespan=lifespan,
    # A hosted deployment publishes no API description and answers only its own pages.
    docs_url=None if hosted.front_door() else "/docs",
    redoc_url=None if hosted.front_door() else "/redoc",
    openapi_url=None if hosted.front_door() else "/openapi.json",
)

if not hosted.front_door():
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(auth.router)
app.include_router(settings.router)
app.include_router(projects.router)
app.include_router(hints.router)
app.include_router(intents.router)
app.include_router(export.router)
app.include_router(attestation.router)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True, "service": "cairn", "version": __version__}


@app.get("/model", include_in_schema=False)
def model():
    """Which model the workers run on, when it is connected outside Cairn. Never a key."""

    if not confidential.managed():
        return {"managed": False}
    try:
        models = confidential.connected_models()
    except confidential.ConfidentialBootstrapError:
        return {"managed": True, "ready": False, "models": []}
    if models is None:
        return {"managed": True, "ready": True, "models": []}
    return {"managed": True, "ready": bool(models), "models": models}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
