from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile
from fastapi.responses import FileResponse

from app.config import Settings, get_settings
from app.services.conversion_service import ConversionService
from app.utils.files import cleanup_job_dir, cleanup_paths

router = APIRouter(tags=["conversion"])


def get_conversion_service(settings: Settings = Depends(get_settings)) -> ConversionService:
    return ConversionService(settings=settings)


@router.post(
    "/convert-to-pdf",
    summary="Convert To Pdf",
    description="Upload one supported office document as multipart/form-data with field name `file`, then receive a PDF file stream.",
)
async def convert_to_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    service: ConversionService = Depends(get_conversion_service),
) -> FileResponse:
    result = await service.convert_file_to_pdf(file)
    background_tasks.add_task(cleanup_job_dir, result.job_dir)
    return FileResponse(
        path=result.output_path,
        media_type="application/pdf",
        filename=result.output_filename,
        background=background_tasks,
    )


@router.post(
    "/convert-to-pdf/batch",
    summary="Convert To Pdf Batch",
    description="Upload multiple supported office documents as multipart/form-data. Repeat the `files` field for each file, then receive a ZIP archive.",
)
async def convert_to_pdf_batch(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    service: ConversionService = Depends(get_conversion_service),
) -> FileResponse:
    result = await service.convert_files_to_pdf_batch(files)
    background_tasks.add_task(cleanup_paths, result.cleanup_paths)
    return FileResponse(
        path=result.zip_path,
        media_type="application/zip",
        filename=f"{result.batch_id}.zip",
        background=background_tasks,
    )

