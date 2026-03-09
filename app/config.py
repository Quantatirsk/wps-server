from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import os


def _get_csv_env(name: str) -> tuple[str, ...]:
    value = os.getenv(name, "")
    parts = [item.strip().rstrip("/") for item in value.split(",")]
    return tuple(item for item in parts if item)


@dataclass(frozen=True)
class Settings:
    api_prefix: str
    service_name: str
    workspace_root: Path
    jobs_dir: Path
    runtime_dir: Path
    conversion_timeout_seconds: int
    cleanup_max_age_seconds: int
    max_upload_size_bytes: int
    batch_max_files: int
    batch_worker_urls: tuple[str, ...]
    dispatcher_request_timeout_seconds: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    workspace_root = Path(os.getenv("WPS_WORKSPACE_ROOT", "/workspace"))
    jobs_dir = workspace_root / "jobs"
    runtime_dir = workspace_root / "runtime"
    return Settings(
        api_prefix="/api/v1",
        service_name="wps-api-service",
        workspace_root=workspace_root,
        jobs_dir=jobs_dir,
        runtime_dir=runtime_dir,
        conversion_timeout_seconds=int(
            os.getenv("WPS_CONVERSION_TIMEOUT_SECONDS", "120")
        ),
        cleanup_max_age_seconds=int(
            os.getenv("WPS_CLEANUP_MAX_AGE_SECONDS", str(24 * 60 * 60))
        ),
        max_upload_size_bytes=int(
            os.getenv("WPS_MAX_UPLOAD_SIZE_BYTES", str(50 * 1024 * 1024))
        ),
        batch_max_files=int(os.getenv("WPS_BATCH_MAX_FILES", "10")),
        batch_worker_urls=_get_csv_env("WPS_BATCH_WORKER_URLS"),
        dispatcher_request_timeout_seconds=int(
            os.getenv("WPS_DISPATCHER_REQUEST_TIMEOUT_SECONDS", "180")
        ),
    )
