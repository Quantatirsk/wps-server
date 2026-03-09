from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.convert_routes import router as convert_router
from app.api.health_routes import router as health_router
from app.config import get_settings
from app.utils.errors import AppError
from app.utils.files import cleanup_expired_jobs, ensure_runtime_directories
from app.utils.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    settings = get_settings()
    ensure_runtime_directories(settings)
    deleted_count = cleanup_expired_jobs(
        settings.jobs_dir, settings.cleanup_max_age_seconds
    )
    logger.info("startup_cleanup deleted_jobs=%s", deleted_count)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(title=settings.service_name, lifespan=lifespan)
    application.openapi_version = "3.0.3"

    @application.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_body())

    @application.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unexpected_error: %s", exc)
        error = AppError("unexpected internal error")
        return JSONResponse(status_code=500, content=error.to_body())

    application.include_router(health_router, prefix=settings.api_prefix)
    application.include_router(convert_router, prefix=settings.api_prefix)
    return application


app = create_app()
