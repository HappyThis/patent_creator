from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..core import ApiError, Settings, setup_logging
from ..schemas import ErrorEnvelope
from ..services import AppServices
from .routes import (
    create_chat_router,
    create_document_router,
    create_export_router,
    create_project_router,
)

logger = logging.getLogger("patent_creator.api")


def create_app(settings: Settings | None = None, services: AppServices | None = None) -> FastAPI:
    active_settings = settings or Settings.from_env()
    setup_logging(
        active_settings.log_dir,
        active_settings.log_level,
        backup_count=active_settings.log_backup_days,
    )
    active_services = services or AppServices(active_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        active_services.settings.data_dir.mkdir(parents=True, exist_ok=True)
        recovered_projects = active_services.store.recover_interrupted_projects()
        if recovered_projects:
            logger.warning(
                "recovered interrupted projects count=%d ids=%s",
                len(recovered_projects),
                ",".join(project.project_id for project in recovered_projects),
            )
        yield

    app = FastAPI(title="Patent Creator Backend", version="0.1.0", lifespan=lifespan)
    app.state.services = active_services
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_settings.cors_allow_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
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
