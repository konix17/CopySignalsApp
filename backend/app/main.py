"""Copy Signals web app: `create_app()` builds it; `app` is what uvicorn serves."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import db, users
from .config import ROOT, Settings, settings
from .logs import setup_logging
from .pipeline import Pipeline
from .routes_admin import router as admin_router
from .routes_app import router as app_router
from .routes_auth import router as auth_router
from .security import SecretBox
from .web import Ctx, security_middleware, unexpected_error, validation_error

log = logging.getLogger(__name__)


def create_app(cfg: Settings = settings, background: bool = True) -> FastAPI:
    """`background=False` skips the data-collection loops (used by tests)."""
    if background:
        setup_logging(cfg.log_dir)
    conn = db.connect(cfg.db_path)
    users.init_secrets(SecretBox(cfg.secret_key_path))
    pipeline = Pipeline(conn, cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if background:
            tasks = [asyncio.create_task(pipeline.run_forever()), asyncio.create_task(pipeline.stream.run()),
                     asyncio.create_task(pipeline.run_bots()), asyncio.create_task(pipeline.run_longshort())]
        yield
        for t in tasks:
            t.cancel()

    # No auto-generated API docs in production: they'd describe every endpoint to anyone.
    app = FastAPI(title="Copy Signals", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = Ctx(conn=conn, settings=cfg, pipeline=pipeline)
    app.middleware("http")(security_middleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[*cfg.allowed_hosts, "testserver"])
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(Exception, unexpected_error)
    app.include_router(auth_router)
    app.include_router(app_router)
    app.include_router(admin_router)
    app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
    return app


def __getattr__(name: str):
    """`uvicorn app.main:app` builds the app on first access, so importing this module (e.g. in tests) has no
    side effects on the real database."""
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
