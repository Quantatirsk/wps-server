from __future__ import annotations

import asyncio
from dataclasses import dataclass
from itertools import count
import multiprocessing
from multiprocessing.connection import Connection
from pathlib import Path
import time
from typing import Any

from app.adapters.base import BaseWpsAdapter
from app.adapters.presentation_adapter import PresentationAdapter
from app.adapters.spreadsheet_adapter import SpreadsheetAdapter
from app.adapters.writer_adapter import WriterAdapter
from app.config import Settings
from app.utils.cpu import (
    ProcessCpuSample,
    sample_process_cpu_percent,
    supports_process_cpu_sampling,
)
from app.utils.errors import (
    AppError,
    ConversionTimeoutError,
    WpsConversionError,
    WpsOpenDocumentError,
    WpsStartupError,
)
from app.utils.logging import get_logger


FAMILY_WRITER = "writer"
FAMILY_PRESENTATION = "presentation"
FAMILY_SPREADSHEET = "spreadsheet"

ERROR_TYPES: dict[str, type[AppError]] = {
    "WpsStartupError": WpsStartupError,
    "WpsOpenDocumentError": WpsOpenDocumentError,
    "WpsConversionError": WpsConversionError,
    "ConversionTimeoutError": ConversionTimeoutError,
}
PREWARM_FAMILY_ORDER = (
    FAMILY_WRITER,
    FAMILY_SPREADSHEET,
    FAMILY_PRESENTATION,
)
JANITOR_INTERVAL_SECONDS = 15
HOT_IDLE_CPU_PERCENT = 80.0
HOT_IDLE_MIN_IDLE_SECONDS = 30
HOT_IDLE_REQUIRED_SAMPLES = 2

_MANAGER: WarmSessionManager | None = None


@dataclass(frozen=True)
class WarmConversionResult:
    process_pid: int | None
    warm_hit: bool


class FamilyWorker:
    def __init__(
        self,
        family: str,
        worker_index: int,
        settings: Settings,
        startup_lock: Any,
    ) -> None:
        self.family = family
        self.settings = settings
        self._startup_lock = startup_lock
        self.worker_name = f"{family}-{worker_index}"
        self.logger = get_logger(f"{__name__}.{self.worker_name}")
        self._ctx = multiprocessing.get_context("spawn")
        self._lock = asyncio.Lock()
        self._parent_conn: Connection | None = None
        self._process: multiprocessing.Process | None = None
        self._last_used_monotonic: float | None = None
        self._session_process_pid: int | None = None
        self._cpu_sample: ProcessCpuSample | None = None
        self._hot_idle_sample_count = 0

    async def convert(
        self,
        input_path: Path,
        output_path: Path,
        timeout_seconds: int,
    ) -> WarmConversionResult:
        async with self._lock:
            await self._refresh_if_idle(timeout_seconds)
            self._ensure_process()
            started_at = time.perf_counter()
            try:
                response = await asyncio.to_thread(
                    self._send_convert_request,
                    input_path,
                    output_path,
                    timeout_seconds,
                )
            except AppError:
                self.logger.exception(
                    "warm_convert_failed family=%s input=%s output=%s",
                    self.family,
                    input_path,
                    output_path,
                )
                self._shutdown_process(force=True)
                raise
            except Exception as exc:
                self.logger.exception(
                    "warm_convert_failed family=%s input=%s output=%s",
                    self.family,
                    input_path,
                    output_path,
                )
                self._shutdown_process(force=True)
                raise WpsConversionError(
                    f"{self.family} warm session failed: {exc}"
                ) from exc

            self._mark_session_alive(response["processPid"])
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            self.logger.info(
                "warm_convert_succeeded family=%s warm_hit=%s duration_ms=%s process_pid=%s",
                self.family,
                response["warmHit"],
                duration_ms,
                response["processPid"],
            )
            return WarmConversionResult(
                process_pid=response["processPid"],
                warm_hit=response["warmHit"],
            )

    async def prewarm(self, timeout_seconds: int) -> None:
        async with self._lock:
            if await self._refresh_if_idle(timeout_seconds):
                return
            self._ensure_process()
            started_at = time.perf_counter()
            try:
                response = await asyncio.to_thread(
                    self._send_prewarm_request,
                    timeout_seconds,
                )
            except AppError:
                self.logger.exception("warm_prewarm_failed family=%s", self.family)
                self._shutdown_process(force=True)
                raise
            except Exception as exc:
                self.logger.exception("warm_prewarm_failed family=%s", self.family)
                self._shutdown_process(force=True)
                raise WpsStartupError(
                    f"{self.family} warm prewarm failed: {exc}"
                ) from exc

            self._mark_session_alive(response["processPid"])
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            self.logger.info(
                "warm_prewarm_succeeded family=%s duration_ms=%s",
                self.family,
                duration_ms,
            )

    async def run_maintenance(self, timeout_seconds: int) -> None:
        if self._lock.locked():
            return

        async with self._lock:
            if await self._refresh_if_idle(timeout_seconds):
                return
            await self._recycle_if_hot_idle(timeout_seconds)

    def close(self) -> None:
        self._shutdown_process(force=False)

    def _ensure_process(self) -> None:
        if (
            self._process is not None
            and self._process.is_alive()
            and self._parent_conn is not None
        ):
            return

        self._shutdown_process(force=True)
        parent_conn, child_conn = self._ctx.Pipe()
        process = self._ctx.Process(
            target=run_warm_session_worker,
            args=(
                self.family,
                self.worker_name,
                child_conn,
                self.settings.warm_session_max_jobs,
                self._startup_lock,
            ),
            daemon=True,
        )
        process.start()
        child_conn.close()
        self._parent_conn = parent_conn
        self._process = process
        self.logger.info(
            "warm_worker_started family=%s worker_name=%s worker_pid=%s",
            self.family,
            self.worker_name,
            process.pid,
        )

    def _send_convert_request(
        self,
        input_path: Path,
        output_path: Path,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        response = self._send_request(
            {
                "type": "convert",
                "inputPath": str(input_path),
                "outputPath": str(output_path),
            },
            timeout_seconds,
        )
        if not response.get("ok", False):
            error_type = str(response.get("errorType", "WpsConversionError"))
            message = str(response.get("message", "warm worker conversion failed"))
            raise self._build_error(error_type, message)

        return response

    def _send_prewarm_request(self, timeout_seconds: int) -> dict[str, Any]:
        response = self._send_request({"type": "prewarm"}, timeout_seconds)
        if not response.get("ok", False):
            error_type = str(response.get("errorType", "WpsStartupError"))
            message = str(response.get("message", "warm worker prewarm failed"))
            raise self._build_error(error_type, message)
        return response

    def _send_request(
        self,
        payload: dict[str, str],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        conn = self._require_connection()
        process = self._require_process()
        if not process.is_alive():
            raise WpsStartupError(f"{self.family} warm worker is not running")

        try:
            conn.send(payload)
        except (BrokenPipeError, EOFError, OSError) as exc:
            raise WpsStartupError(
                f"{self.family} warm worker request channel is unavailable"
            ) from exc

        if not conn.poll(timeout_seconds):
            raise ConversionTimeoutError(
                f"{self.family} request timed out after {timeout_seconds} seconds"
            )

        try:
            response = conn.recv()
        except EOFError as exc:
            raise WpsStartupError(
                f"{self.family} warm worker exited before replying"
            ) from exc

        if not isinstance(response, dict):
            raise WpsConversionError(
                f"{self.family} warm worker returned an invalid response"
            )

        return response

    def _build_error(self, error_type: str, message: str) -> AppError:
        error_cls = ERROR_TYPES.get(error_type, WpsConversionError)
        return error_cls(message)

    async def _refresh_if_idle(self, timeout_seconds: int) -> bool:
        if self._last_used_monotonic is None:
            return False
        idle_seconds = time.monotonic() - self._last_used_monotonic
        if idle_seconds <= self.settings.warm_session_idle_ttl_seconds:
            return False
        self.logger.info(
            "warm_worker_recycled_idle family=%s idle_seconds=%.2f",
            self.family,
            idle_seconds,
        )
        self._shutdown_process(force=False)
        try:
            self._ensure_process()
            response = await asyncio.to_thread(
                self._send_prewarm_request,
                timeout_seconds,
            )
        except Exception:
            self._shutdown_process(force=True)
            raise
        self._mark_session_alive(response["processPid"])
        self.logger.info(
            "warm_worker_rewarmed_idle family=%s worker_name=%s process_pid=%s",
            self.family,
            self.worker_name,
            response["processPid"],
        )
        return True

    async def _recycle_if_hot_idle(self, timeout_seconds: int) -> None:
        if not supports_process_cpu_sampling():
            return
        if self._process is None or self._session_process_pid is None:
            self._reset_cpu_watch()
            return
        if self._last_used_monotonic is None:
            self._reset_cpu_watch()
            return
        idle_seconds = time.monotonic() - self._last_used_monotonic
        if idle_seconds < HOT_IDLE_MIN_IDLE_SECONDS:
            self._reset_cpu_watch()
            return

        current_sample, cpu_percent = sample_process_cpu_percent(
            self._session_process_pid,
            self._cpu_sample,
        )
        self._cpu_sample = current_sample
        if current_sample is None:
            self.logger.info(
                "warm_worker_recycled_missing_process family=%s session_pid=%s",
                self.family,
                self._session_process_pid,
            )
            self._shutdown_process(force=True)
            return
        if cpu_percent is None:
            return
        if cpu_percent < HOT_IDLE_CPU_PERCENT:
            self._hot_idle_sample_count = 0
            return

        self._hot_idle_sample_count += 1
        self.logger.warning(
            "warm_worker_hot_idle_detected family=%s worker_name=%s session_pid=%s idle_seconds=%.2f cpu_percent=%.2f sample_count=%s",
            self.family,
            self.worker_name,
            self._session_process_pid,
            idle_seconds,
            cpu_percent,
            self._hot_idle_sample_count,
        )
        if self._hot_idle_sample_count < HOT_IDLE_REQUIRED_SAMPLES:
            return

        self.logger.warning(
            "warm_worker_restarting_hot_idle family=%s worker_name=%s session_pid=%s",
            self.family,
            self.worker_name,
            self._session_process_pid,
        )
        self._shutdown_process(force=True)
        try:
            self._ensure_process()
            response = await asyncio.to_thread(
                self._send_prewarm_request,
                timeout_seconds,
            )
        except Exception:
            self._shutdown_process(force=True)
            raise
        self._mark_session_alive(response["processPid"])

    def _mark_session_alive(self, process_pid: int | None) -> None:
        self._last_used_monotonic = time.monotonic()
        self._session_process_pid = process_pid
        self._cpu_sample = None
        self._hot_idle_sample_count = 0

    def _reset_cpu_watch(self) -> None:
        self._cpu_sample = None
        self._hot_idle_sample_count = 0

    def _shutdown_process(self, force: bool) -> None:
        conn = self._parent_conn
        process = self._process
        self._parent_conn = None
        self._process = None
        self._last_used_monotonic = None
        self._session_process_pid = None
        self._cpu_sample = None
        self._hot_idle_sample_count = 0

        if conn is not None:
            if not force:
                try:
                    conn.send({"type": "shutdown"})
                except (BrokenPipeError, EOFError, OSError):
                    pass
            conn.close()

        if process is None:
            return

        process.join(timeout=1 if not force else 0.2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)

    def _require_connection(self) -> Connection:
        if self._parent_conn is None:
            raise WpsStartupError(f"{self.family} warm worker connection is missing")
        return self._parent_conn

    def _require_process(self) -> multiprocessing.Process:
        if self._process is None:
            raise WpsStartupError(f"{self.family} warm worker process is missing")
        return self._process


class FamilyWorkerPool:
    def __init__(
        self,
        family: str,
        worker_count: int,
        settings: Settings,
        startup_lock: Any,
    ) -> None:
        self.family = family
        self.logger = get_logger(f"{__name__}.{family}.pool")
        self._workers = [
            FamilyWorker(family, worker_index, settings, startup_lock)
            for worker_index in range(1, worker_count + 1)
        ]
        self._cursor = count()

    async def convert(
        self,
        input_path: Path,
        output_path: Path,
        timeout_seconds: int,
    ) -> WarmConversionResult:
        worker = self._workers[next(self._cursor) % len(self._workers)]
        self.logger.info(
            "warm_pool_dispatch family=%s worker_name=%s input=%s",
            self.family,
            worker.worker_name,
            input_path.name,
        )
        return await worker.convert(input_path, output_path, timeout_seconds)

    async def prewarm_all(self, timeout_seconds: int) -> None:
        for worker in self._workers:
            await worker.prewarm(timeout_seconds)

    async def run_maintenance(self, timeout_seconds: int) -> None:
        for worker in self._workers:
            await worker.run_maintenance(timeout_seconds)

    def close(self) -> None:
        for worker in self._workers:
            worker.close()


class WarmSessionManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = get_logger(__name__)
        ctx = multiprocessing.get_context("spawn")
        startup_lock = ctx.Lock()
        self._janitor_stop_event = asyncio.Event()
        self._janitor_task: asyncio.Task[None] | None = None
        self._pools = {
            FAMILY_WRITER: FamilyWorkerPool(
                FAMILY_WRITER,
                settings.writer_worker_count,
                settings,
                startup_lock,
            ),
            FAMILY_PRESENTATION: FamilyWorkerPool(
                FAMILY_PRESENTATION,
                1,
                settings,
                startup_lock,
            ),
            FAMILY_SPREADSHEET: FamilyWorkerPool(
                FAMILY_SPREADSHEET,
                1,
                settings,
                startup_lock,
            ),
        }

    async def convert(
        self,
        family: str,
        input_path: Path,
        output_path: Path,
        timeout_seconds: int,
    ) -> WarmConversionResult:
        pool = self._pools.get(family)
        if pool is None:
            raise WpsConversionError(f"unsupported warm session family: {family}")
        return await pool.convert(input_path, output_path, timeout_seconds)

    async def prewarm_all(self, timeout_seconds: int) -> None:
        for family in PREWARM_FAMILY_ORDER:
            await self._pools[family].prewarm_all(timeout_seconds)

    async def start(self) -> None:
        if self._janitor_task is not None:
            return
        self._janitor_stop_event.clear()
        self._janitor_task = asyncio.create_task(
            self._run_janitor_loop(),
            name="warm-session-janitor",
        )

    async def aclose(self) -> None:
        task = self._janitor_task
        if task is not None:
            self._janitor_stop_event.set()
            await task
            self._janitor_task = None
        self.close()

    def close(self) -> None:
        for pool in self._pools.values():
            pool.close()

    async def _run_janitor_loop(self) -> None:
        self.logger.info(
            "warm_session_janitor_started interval_seconds=%s",
            JANITOR_INTERVAL_SECONDS,
        )
        try:
            while not self._janitor_stop_event.is_set():
                try:
                    await self._run_maintenance_cycle()
                except Exception:
                    self.logger.exception("warm_session_janitor_cycle_failed")

                try:
                    await asyncio.wait_for(
                        self._janitor_stop_event.wait(),
                        timeout=JANITOR_INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    continue
        finally:
            self.logger.info("warm_session_janitor_stopped")

    async def _run_maintenance_cycle(self) -> None:
        for pool in self._pools.values():
            await pool.run_maintenance(self.settings.conversion_timeout_seconds)


def get_warm_session_manager(settings: Settings) -> WarmSessionManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = WarmSessionManager(settings)
    return _MANAGER


async def close_warm_session_manager() -> None:
    global _MANAGER
    if _MANAGER is None:
        return
    await _MANAGER.aclose()
    _MANAGER = None


def run_warm_session_worker(
    family: str,
    worker_name: str,
    connection: Connection,
    max_jobs_per_session: int,
    startup_lock: Any,
) -> None:
    logger = get_logger(f"{__name__}.worker.{worker_name}")
    adapter = _build_adapter(family)
    session = None
    jobs_completed = 0
    logger.info("warm_worker_booted family=%s worker_name=%s", family, worker_name)
    try:
        while True:
            try:
                command = connection.recv()
            except EOFError:
                break

            command_type = command.get("type")
            if command_type == "shutdown":
                break
            if command_type == "prewarm":
                session, jobs_completed = _handle_prewarm_command(
                    adapter=adapter,
                    connection=connection,
                    startup_lock=startup_lock,
                    logger=logger,
                    family=family,
                    worker_name=worker_name,
                    session=session,
                    jobs_completed=jobs_completed,
                )
                continue
            if command_type != "convert":
                _send_worker_error_response(
                    connection=connection,
                    error_type="WpsConversionError",
                    message=f"unsupported command: {command_type}",
                )
                continue

            session, jobs_completed = _handle_convert_command(
                adapter=adapter,
                connection=connection,
                startup_lock=startup_lock,
                logger=logger,
                family=family,
                worker_name=worker_name,
                command=command,
                session=session,
                jobs_completed=jobs_completed,
                max_jobs_per_session=max_jobs_per_session,
            )
    finally:
        if session is not None:
            _stop_session_safely(adapter, session)
        connection.close()
        logger.info("warm_worker_stopped family=%s worker_name=%s", family, worker_name)


def _build_adapter(family: str) -> BaseWpsAdapter:
    if family == FAMILY_WRITER:
        return WriterAdapter()
    if family == FAMILY_PRESENTATION:
        return PresentationAdapter()
    if family == FAMILY_SPREADSHEET:
        return SpreadsheetAdapter()
    raise WpsConversionError(f"unsupported family: {family}")


def _stop_session_safely(adapter: BaseWpsAdapter, session: Any) -> None:
    try:
        adapter.stop_session(session)
    except Exception:
        pass


def _handle_prewarm_command(
    adapter: BaseWpsAdapter,
    connection: Connection,
    startup_lock: Any,
    logger: Any,
    family: str,
    worker_name: str,
    session: Any,
    jobs_completed: int,
) -> tuple[Any, int]:
    try:
        if session is None:
            session = _start_worker_session(
                adapter=adapter,
                startup_lock=startup_lock,
                logger=logger,
                family=family,
                worker_name=worker_name,
                phase="prewarm",
            )
            jobs_completed = 0
        else:
            logger.info(
                "warm_worker_prewarm_hit family=%s worker_name=%s",
                family,
                worker_name,
            )
        connection.send({"ok": True, "processPid": session.process_pid})
        return session, jobs_completed
    except AppError as exc:
        logger.exception("warm_worker_prewarm_failed family=%s", family)
        _send_worker_error_response(connection, exc.__class__.__name__, str(exc))
    except Exception as exc:
        logger.exception("warm_worker_prewarm_failed family=%s", family)
        _send_worker_error_response(connection, "WpsStartupError", str(exc))

    return _recycle_worker_session(adapter, session)


def _handle_convert_command(
    adapter: BaseWpsAdapter,
    connection: Connection,
    startup_lock: Any,
    logger: Any,
    family: str,
    worker_name: str,
    command: dict[str, str],
    session: Any,
    jobs_completed: int,
    max_jobs_per_session: int,
) -> tuple[Any, int]:
    input_path = Path(str(command["inputPath"]))
    output_path = Path(str(command["outputPath"]))
    warm_hit = session is not None

    try:
        if session is None:
            session = _start_worker_session(
                adapter=adapter,
                startup_lock=startup_lock,
                logger=logger,
                family=family,
                worker_name=worker_name,
                phase="cold_start",
                input_name=input_path.name,
            )
            jobs_completed = 0
            details = adapter.convert_with_session(session, input_path, output_path)
        else:
            details = adapter.convert_with_session(session, input_path, output_path)

        jobs_completed += 1
        connection.send(
            {
                "ok": True,
                "processPid": details.process_pid,
                "warmHit": warm_hit,
            }
        )
        if jobs_completed >= max_jobs_per_session:
            logger.info(
                "warm_worker_recycled_jobs family=%s worker_name=%s jobs_completed=%s",
                family,
                worker_name,
                jobs_completed,
            )
            return _recycle_worker_session(adapter, session)
        return session, jobs_completed
    except AppError as exc:
        logger.exception(
            "warm_worker_convert_failed family=%s warm_hit=%s input=%s",
            family,
            warm_hit,
            input_path,
        )
        _send_worker_error_response(connection, exc.__class__.__name__, str(exc))
    except Exception as exc:
        logger.exception(
            "warm_worker_convert_failed family=%s warm_hit=%s input=%s",
            family,
            warm_hit,
            input_path,
        )
        _send_worker_error_response(connection, "WpsConversionError", str(exc))

    return _recycle_worker_session(adapter, session)


def _recycle_worker_session(adapter: BaseWpsAdapter, session: Any) -> tuple[None, int]:
    if session is not None:
        _stop_session_safely(adapter, session)
    return None, 0


def _start_worker_session(
    adapter: BaseWpsAdapter,
    startup_lock: Any,
    logger: Any,
    family: str,
    worker_name: str,
    phase: str,
    input_name: str | None = None,
) -> Any:
    _log_worker_session_event(
        logger=logger,
        action=f"{phase}_wait",
        family=family,
        worker_name=worker_name,
        input_name=input_name,
    )
    with startup_lock:
        _log_worker_session_event(
            logger=logger,
            action=f"{phase}_begin",
            family=family,
            worker_name=worker_name,
            input_name=input_name,
        )
        session = adapter.start_session()
    _log_worker_session_event(
        logger=logger,
        action=f"{phase}_complete",
        family=family,
        worker_name=worker_name,
        input_name=input_name,
    )
    return session


def _log_worker_session_event(
    logger: Any,
    action: str,
    family: str,
    worker_name: str,
    input_name: str | None = None,
) -> None:
    event_name = f"warm_worker_{action}"
    if input_name is None:
        logger.info("%s family=%s worker_name=%s", event_name, family, worker_name)
        return
    logger.info(
        "%s family=%s worker_name=%s input=%s",
        event_name,
        family,
        worker_name,
        input_name,
    )


def _send_worker_error_response(
    connection: Connection,
    error_type: str,
    message: str,
) -> None:
    connection.send(
        {
            "ok": False,
            "errorType": error_type,
            "message": message,
        }
    )
