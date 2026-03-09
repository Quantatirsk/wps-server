from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import asyncio
import time

import httpx
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
class PreparedConversionJob:
    route: ConversionRoute
    job_paths: JobPaths
    input_filename: str
    output_filename: str
    input_size: int


@dataclass(frozen=True)
class ConversionJobResult:
    job_id: str
    job_dir: Path
    output_path: Path
    input_filename: str
    output_filename: str
    document_family: str
    queue_wait_ms: int
    convert_ms: int
    duration_ms: int
    process_pid: int | None
    worker_url: str | None


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
        prepared_job = await self._prepare_job(upload_file)
        return await self._run_conversion(prepared_job)

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

        prepared_jobs = [
            await self._prepare_job(upload_file) for upload_file in upload_files
        ]

        if self.settings.batch_worker_urls:
            return await self._convert_files_to_pdf_batch_via_workers(prepared_jobs)

        results = await asyncio.gather(
            *(self._run_conversion(prepared_job) for prepared_job in prepared_jobs),
            return_exceptions=True,
        )
        return self._build_batch_result(results)

    async def _prepare_job(self, upload_file: UploadFile) -> PreparedConversionJob:
        route = self._get_route_or_raise(upload_file.filename)
        job_paths = build_job_paths(self.settings, upload_file.filename)
        output_filename = f"{get_safe_stem(upload_file.filename)}.pdf"

        try:
            size = await persist_upload_file(upload_file, job_paths.input_path)
            self._validate_file_size(size, job_paths)
            return PreparedConversionJob(
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

    async def _convert_files_to_pdf_batch_via_workers(
        self,
        prepared_jobs: list[PreparedConversionJob],
    ) -> BatchConversionResult:
        indexed_jobs = list(enumerate(prepared_jobs))
        worker_urls = self.settings.batch_worker_urls
        buckets: list[list[tuple[int, PreparedConversionJob]]] = [
            [] for _ in worker_urls
        ]
        for index, prepared_job in indexed_jobs:
            bucket_index = index % len(worker_urls)
            buckets[bucket_index].append((index, prepared_job))

        timeout = httpx.Timeout(self.settings.dispatcher_request_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            bucket_results = await asyncio.gather(
                *(
                    self._dispatch_bucket(client, worker_urls[index], bucket)
                    for index, bucket in enumerate(buckets)
                    if bucket
                ),
                return_exceptions=True,
            )

        exceptions = [result for result in bucket_results if isinstance(result, Exception)]
        successful_pairs = [
            pair
            for result in bucket_results
            if isinstance(result, list)
            for pair in result
        ]
        if exceptions:
            cleanup_paths([result.job_dir for _, result in successful_pairs])
            raise exceptions[0]

        successful_pairs.sort(key=lambda item: item[0])
        ordered_results = [result for _, result in successful_pairs]
        return self._build_batch_result(ordered_results)

    async def _dispatch_bucket(
        self,
        client: httpx.AsyncClient,
        worker_url: str,
        bucket: list[tuple[int, PreparedConversionJob]],
    ) -> list[tuple[int, ConversionJobResult]]:
        results: list[tuple[int, ConversionJobResult]] = []
        for index, prepared_job in bucket:
            result = await self._dispatch_to_worker(client, worker_url, prepared_job)
            results.append((index, result))
        return results

    async def _dispatch_to_worker(
        self,
        client: httpx.AsyncClient,
        worker_url: str,
        prepared_job: PreparedConversionJob,
    ) -> ConversionJobResult:
        started_at = time.perf_counter()
        endpoint = self._build_worker_convert_url(worker_url)
        self.logger.info(
            "dispatch_started job_id=%s worker_url=%s file=%s family=%s size=%s",
            prepared_job.job_paths.job_id,
            worker_url,
            prepared_job.input_filename,
            prepared_job.route.document_family,
            prepared_job.input_size,
        )

        try:
            with prepared_job.job_paths.input_path.open("rb") as input_file:
                response = await client.post(
                    endpoint,
                    files={
                        "file": (
                            prepared_job.input_filename,
                            input_file,
                            "application/octet-stream",
                        )
                    },
                )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            cleanup_job_dir(prepared_job.job_paths.job_dir)
            raise ConversionTimeoutError(
                f"worker request timed out: {worker_url}"
            ) from exc
        except httpx.HTTPError as exc:
            cleanup_job_dir(prepared_job.job_paths.job_dir)
            raise WpsConversionError(
                f"worker request failed: {worker_url}: {exc}"
            ) from exc

        prepared_job.job_paths.output_path.write_bytes(response.content)
        if not prepared_job.job_paths.output_path.exists():
            cleanup_job_dir(prepared_job.job_paths.job_dir)
            raise WpsConversionError("dispatched conversion completed without output file")

        finished_at = time.perf_counter()
        convert_ms = int((finished_at - started_at) * 1000)
        result = ConversionJobResult(
            job_id=prepared_job.job_paths.job_id,
            job_dir=prepared_job.job_paths.job_dir,
            output_path=prepared_job.job_paths.output_path,
            input_filename=prepared_job.input_filename,
            output_filename=prepared_job.output_filename,
            document_family=prepared_job.route.document_family,
            queue_wait_ms=0,
            convert_ms=convert_ms,
            duration_ms=convert_ms,
            process_pid=None,
            worker_url=worker_url,
        )
        write_job_metadata(
            prepared_job.job_paths,
            {
                "jobId": result.job_id,
                "documentFamily": result.document_family,
                "inputFilename": result.input_filename,
                "inputSize": prepared_job.input_size,
                "outputFilename": result.output_filename,
                "outputSize": get_file_size(result.output_path),
                "queueWaitMs": result.queue_wait_ms,
                "convertMs": result.convert_ms,
                "durationMs": result.duration_ms,
                "processPid": result.process_pid,
                "workerUrl": result.worker_url,
                "status": "succeeded",
            },
        )
        self.logger.info(
            "dispatch_succeeded job_id=%s worker_url=%s family=%s duration_ms=%s",
            result.job_id,
            worker_url,
            result.document_family,
            result.duration_ms,
        )
        return result

    async def _run_conversion(
        self,
        prepared_job: PreparedConversionJob,
    ) -> ConversionJobResult:
        started_at = time.perf_counter()
        self.logger.info(
            "conversion_started job_id=%s file=%s family=%s size=%s",
            prepared_job.job_paths.job_id,
            prepared_job.input_filename,
            prepared_job.route.document_family,
            prepared_job.input_size,
        )

        try:
            async with prepared_job.route.lock:
                lock_acquired_at = time.perf_counter()
                details = await asyncio.wait_for(
                    asyncio.to_thread(
                        prepared_job.route.adapter.convert_to_pdf,
                        prepared_job.job_paths.input_path,
                        prepared_job.job_paths.output_path,
                    ),
                    timeout=self.settings.conversion_timeout_seconds,
                )
        except TimeoutError as exc:
            cleanup_job_dir(prepared_job.job_paths.job_dir)
            raise ConversionTimeoutError() from exc
        except Exception:
            cleanup_job_dir(prepared_job.job_paths.job_dir)
            raise

        if not prepared_job.job_paths.output_path.exists():
            cleanup_job_dir(prepared_job.job_paths.job_dir)
            raise WpsConversionError("conversion completed without output file")

        finished_at = time.perf_counter()
        queue_wait_ms = int((lock_acquired_at - started_at) * 1000)
        convert_ms = int((finished_at - lock_acquired_at) * 1000)
        duration_ms = int((finished_at - started_at) * 1000)
        write_job_metadata(
            prepared_job.job_paths,
            {
                "jobId": prepared_job.job_paths.job_id,
                "documentFamily": prepared_job.route.document_family,
                "inputFilename": prepared_job.input_filename,
                "inputSize": get_file_size(prepared_job.job_paths.input_path),
                "outputFilename": prepared_job.output_filename,
                "outputSize": get_file_size(prepared_job.job_paths.output_path),
                "queueWaitMs": queue_wait_ms,
                "convertMs": convert_ms,
                "durationMs": duration_ms,
                "processPid": details.process_pid,
                "workerUrl": None,
                "status": "succeeded",
            },
        )
        self.logger.info(
            "conversion_succeeded job_id=%s family=%s queue_wait_ms=%s convert_ms=%s duration_ms=%s pid=%s",
            prepared_job.job_paths.job_id,
            prepared_job.route.document_family,
            queue_wait_ms,
            convert_ms,
            duration_ms,
            details.process_pid,
        )
        return ConversionJobResult(
            job_id=prepared_job.job_paths.job_id,
            job_dir=prepared_job.job_paths.job_dir,
            output_path=prepared_job.job_paths.output_path,
            input_filename=prepared_job.input_filename,
            output_filename=prepared_job.output_filename,
            document_family=prepared_job.route.document_family,
            queue_wait_ms=queue_wait_ms,
            convert_ms=convert_ms,
            duration_ms=duration_ms,
            process_pid=details.process_pid,
            worker_url=None,
        )

    def _build_batch_result(
        self,
        results: list[ConversionJobResult] | list[Exception | ConversionJobResult],
    ) -> BatchConversionResult:
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

    def _build_worker_convert_url(self, worker_url: str) -> str:
        if worker_url.endswith(self.settings.api_prefix):
            return f"{worker_url}/convert-to-pdf"
        return f"{worker_url}{self.settings.api_prefix}/convert-to-pdf"

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
                    "workerUrl": result.worker_url,
                    "queueWaitMs": result.queue_wait_ms,
                    "convertMs": result.convert_ms,
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
