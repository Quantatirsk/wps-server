from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import asyncio
import time

from fastapi import UploadFile

from app.adapters.base import BaseWpsAdapter
from app.adapters.presentation_adapter import PresentationAdapter
from app.adapters.spreadsheet_adapter import SpreadsheetAdapter
from app.adapters.writer_adapter import WriterAdapter
from app.config import Settings
from app.utils.errors import (
    ConversionTimeoutError,
    InvalidInputError,
    PayloadTooLargeError,
    UnsupportedFormatError,
    WpsConversionError,
)
from app.utils.files import (
    BatchPaths,
    JobPaths,
    build_batch_paths,
    build_job_paths,
    cleanup_job_dir,
    cleanup_paths,
    create_zip_archive,
    get_file_size,
    get_safe_stem,
    persist_upload_file,
    write_job_metadata,
    write_json_file,
)
from app.utils.locks import (
    get_presentation_lock,
    get_spreadsheet_lock,
    get_writer_lock,
)
from app.utils.logging import get_logger


@dataclass(frozen=True)
class ConversionRoute:
    document_family: str
    adapter: BaseWpsAdapter
    lock: asyncio.Lock


@dataclass(frozen=True)
class ConversionJobResult:
    job_id: str
    job_dir: Path
    output_path: Path
    input_filename: str
    output_filename: str
    document_family: str
    duration_ms: int
    process_pid: int | None


@dataclass(frozen=True)
class BatchConversionResult:
    batch_id: str
    zip_path: Path
    cleanup_paths: list[Path]


WRITER_ROUTE = ConversionRoute(
    document_family="writer",
    adapter=WriterAdapter(),
    lock=get_writer_lock(),
)
PRESENTATION_ROUTE = ConversionRoute(
    document_family="presentation",
    adapter=PresentationAdapter(),
    lock=get_presentation_lock(),
)
SPREADSHEET_ROUTE = ConversionRoute(
    document_family="spreadsheet",
    adapter=SpreadsheetAdapter(),
    lock=get_spreadsheet_lock(),
)

ROUTES_BY_SUFFIX: dict[str, ConversionRoute] = {
    ".doc": WRITER_ROUTE,
    ".docx": WRITER_ROUTE,
    ".ppt": PRESENTATION_ROUTE,
    ".pptx": PRESENTATION_ROUTE,
    ".xls": SPREADSHEET_ROUTE,
    ".xlsx": SPREADSHEET_ROUTE,
}
SUPPORTED_SUFFIXES = ", ".join(sorted(ROUTES_BY_SUFFIX))


class ConversionService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = get_logger(__name__)

    async def convert_file_to_pdf(self, upload_file: UploadFile) -> ConversionJobResult:
        route = self._get_route_or_raise(upload_file.filename)
        job_paths = build_job_paths(self.settings, upload_file.filename)
        output_filename = f"{get_safe_stem(upload_file.filename)}.pdf"

        try:
            size = await persist_upload_file(upload_file, job_paths.input_path)
            self._validate_file_size(size, job_paths)
            return await self._run_conversion(
                route=route,
                job_paths=job_paths,
                input_filename=upload_file.filename or job_paths.input_path.name,
                output_filename=output_filename,
                input_size=size,
            )
        except Exception:
            if job_paths.job_dir.exists() and not job_paths.output_path.exists():
                cleanup_job_dir(job_paths.job_dir)
            raise

    async def convert_files_to_pdf_batch(
        self,
        upload_files: list[UploadFile],
    ) -> BatchConversionResult:
        if not upload_files:
            raise InvalidInputError("at least one file is required")
        if len(upload_files) > self.settings.batch_max_files:
            raise InvalidInputError(
                f"batch supports at most {self.settings.batch_max_files} files"
            )

        for upload_file in upload_files:
            self._get_route_or_raise(upload_file.filename)

        results = await asyncio.gather(
            *(self.convert_file_to_pdf(upload_file) for upload_file in upload_files),
            return_exceptions=True,
        )

        exceptions = [result for result in results if isinstance(result, Exception)]
        successful_results = [
            result for result in results if isinstance(result, ConversionJobResult)
        ]
        if exceptions:
            cleanup_paths([result.job_dir for result in successful_results])
            raise exceptions[0]

        batch_paths = build_batch_paths(self.settings)
        cleanup_targets = [
            batch_paths.batch_dir,
            *[result.job_dir for result in successful_results],
        ]

        try:
            write_json_file(
                batch_paths.manifest_path,
                self._build_batch_manifest(batch_paths.batch_id, successful_results),
            )
            create_zip_archive(
                batch_paths.zip_path,
                self._build_batch_archive_entries(successful_results, batch_paths),
            )
        except Exception:
            cleanup_paths(cleanup_targets)
            raise

        self.logger.info(
            "batch_conversion_succeeded batch_id=%s item_count=%s",
            batch_paths.batch_id,
            len(successful_results),
        )
        return BatchConversionResult(
            batch_id=batch_paths.batch_id,
            zip_path=batch_paths.zip_path,
            cleanup_paths=cleanup_targets,
        )

    async def _run_conversion(
        self,
        *,
        route: ConversionRoute,
        job_paths: JobPaths,
        input_filename: str,
        output_filename: str,
        input_size: int,
    ) -> ConversionJobResult:
        started_at = time.perf_counter()
        self.logger.info(
            "conversion_started job_id=%s file=%s family=%s size=%s",
            job_paths.job_id,
            input_filename,
            route.document_family,
            input_size,
        )

        try:
            async with route.lock:
                details = await asyncio.wait_for(
                    asyncio.to_thread(
                        route.adapter.convert_to_pdf,
                        job_paths.input_path,
                        job_paths.output_path,
                    ),
                    timeout=self.settings.conversion_timeout_seconds,
                )
        except TimeoutError as exc:
            cleanup_job_dir(job_paths.job_dir)
            raise ConversionTimeoutError() from exc
        except Exception:
            cleanup_job_dir(job_paths.job_dir)
            raise

        if not job_paths.output_path.exists():
            cleanup_job_dir(job_paths.job_dir)
            raise WpsConversionError("conversion completed without output file")

        duration_ms = int((time.perf_counter() - started_at) * 1000)
        write_job_metadata(
            job_paths,
            {
                "jobId": job_paths.job_id,
                "documentFamily": route.document_family,
                "inputFilename": input_filename,
                "inputSize": get_file_size(job_paths.input_path),
                "outputFilename": output_filename,
                "outputSize": get_file_size(job_paths.output_path),
                "durationMs": duration_ms,
                "processPid": details.process_pid,
                "status": "succeeded",
            },
        )
        self.logger.info(
            "conversion_succeeded job_id=%s family=%s duration_ms=%s pid=%s",
            job_paths.job_id,
            route.document_family,
            duration_ms,
            details.process_pid,
        )
        return ConversionJobResult(
            job_id=job_paths.job_id,
            job_dir=job_paths.job_dir,
            output_path=job_paths.output_path,
            input_filename=input_filename,
            output_filename=output_filename,
            document_family=route.document_family,
            duration_ms=duration_ms,
            process_pid=details.process_pid,
        )

    def _get_route_or_raise(self, filename: str | None) -> ConversionRoute:
        suffix = Path(filename or "").suffix.lower()
        route = ROUTES_BY_SUFFIX.get(suffix)
        if route is None:
            raise UnsupportedFormatError(
                f"unsupported file format, supported formats: {SUPPORTED_SUFFIXES}"
            )
        return route

    def _validate_file_size(self, size: int, job_paths: JobPaths) -> None:
        if size > self.settings.max_upload_size_bytes:
            cleanup_job_dir(job_paths.job_dir)
            raise PayloadTooLargeError("uploaded file exceeds configured size limit")

    def _build_batch_manifest(
        self,
        batch_id: str,
        results: list[ConversionJobResult],
    ) -> dict[str, object]:
        return {
            "batchId": batch_id,
            "itemCount": len(results),
            "items": [
                {
                    "jobId": result.job_id,
                    "documentFamily": result.document_family,
                    "inputFilename": result.input_filename,
                    "outputFilename": result.output_filename,
                    "durationMs": result.duration_ms,
                    "processPid": result.process_pid,
                    "status": "succeeded",
                }
                for result in results
            ],
        }

    def _build_batch_archive_entries(
        self,
        results: list[ConversionJobResult],
        batch_paths: BatchPaths,
    ) -> list[tuple[Path, str]]:
        used_names: set[str] = set()
        entries: list[tuple[Path, str]] = []
        for index, result in enumerate(results, start=1):
            archive_name = self._dedupe_archive_name(
                f"outputs/{result.output_filename}",
                used_names,
                index,
            )
            entries.append((result.output_path, archive_name))
        entries.append((batch_paths.manifest_path, "manifest.json"))
        return entries

    def _dedupe_archive_name(
        self,
        candidate: str,
        used_names: set[str],
        index: int,
    ) -> str:
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate

        path = Path(candidate)
        deduped = f"{path.parent}/{path.stem}_{index}{path.suffix}"
        used_names.add(deduped)
        return deduped
