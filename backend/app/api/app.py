from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..core import ApiError, Settings
from ..schemas import ErrorEnvelope
from ..services import AppServices
from .routes import (
    create_chat_router,
    create_document_router,
    create_export_router,
    create_project_router,
)


def create_app(settings: Settings | None = None, services: AppServices | None = None) -> FastAPI:
    active_settings = settings or Settings.from_env()
    active_services = services or AppServices(active_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        active_services.settings.data_dir.mkdir(parents=True, exist_ok=True)
        yield

    app = FastAPI(title="Patent Creator Backend", version="0.1.0", lifespan=lifespan)
    app.state.services = active_services
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_settings.cors_allow_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(ApiError)
    async def handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        envelope = ErrorEnvelope.model_validate({"error": {"code": exc.code, "message": exc.message}})
        return JSONResponse(status_code=exc.status_code, content=envelope.model_dump())

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(create_project_router(active_services))
    app.include_router(create_document_router(active_services))
    app.include_router(create_chat_router(active_services))
    app.include_router(create_export_router(active_services))
    return app


app = create_app()
