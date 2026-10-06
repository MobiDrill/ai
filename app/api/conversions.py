import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse

from app.schemas.conversion import ErrorResponse
from app.logging_config import job_id_context

router = APIRouter(prefix="/api/v1/conversions", tags=["conversions"])
logger = logging.getLogger(__name__)


class PipelinePdfResponse(FileResponse):
    def __init__(self, result):
        super().__init__(result.path, media_type="application/pdf", filename=result.filename,
                         headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                  "X-Job-ID": result.job_id})


@router.post("/pdf", response_class=FileResponse,
             summary="문서를 PDF로 변환하고 다운로드",
             description=("`multipart/form-data`의 `file` 필드로 문서 1개를 업로드합니다. "
                          "지원 확장자: `.ppt`, `.pptx`, `.docx`, `.pdf`, 구형 `.hwp`. "
                          "HWP 5.x/HWPX와 암호화 문서는 지원하지 않습니다. "
                          "PDF 입력은 검증 후 원본 바이트로 반환하고, 다른 형식은 네트워크가 차단된 "
                          "상시 실행되는 로컬 LibreOffice 변환 서버에서 변환합니다. 성공 시 PDF를 첨부 파일로 다운로드하며 "
                          "원본은 resources/static/<UUID>/0/, PDF는 같은 UUID의 1/에 보관합니다. "
                          "응답 헤더 X-Job-ID로 후속 파이프라인에서 파일을 식별할 수 있습니다. "
                          "처리 제한은 API 소개를 참조하세요."),
             operation_id="convertDocumentToPdf",
             responses={200: {"description": "변환 PDF 또는 검증된 원본 PDF",
                              "headers": {"Content-Disposition": {"description": "attachment; filename=<원본명>.pdf", "schema": {"type": "string"}},
                                          "Cache-Control": {"description": "다운로드 결과 캐시 금지", "schema": {"type": "string", "enum": ["no-store"]}},
                                          "X-Job-ID": {"description": "원본·PDF 및 후속 작업의 저장 디렉터리 UUID", "schema": {"type": "string", "format": "uuid"}}},
                              "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}},
                        400: {"description": "Invalid document", "model": ErrorResponse},
                        403: {"description": "Local clients only", "model": ErrorResponse},
                        413: {"description": "Upload too large", "model": ErrorResponse},
                        415: {"description": "Unsupported format or HWP version", "model": ErrorResponse},
                        422: {"description": "Invalid request or encrypted document", "model": ErrorResponse},
                        500: {"description": "Conversion failed", "model": ErrorResponse},
                        503: {"description": "Offline converter unavailable, busy, or memory limit exceeded", "model": ErrorResponse},
                        504: {"description": "Conversion timeout", "model": ErrorResponse}})
async def convert_to_pdf(request: Request, file: Annotated[UploadFile, File(
    description="변환할 로컬 문서 1개. PPT/PPTX/DOCX/PDF/구형 HWP만 지원합니다.")]):
    service = request.app.state.conversion_service
    logger.debug("conversion endpoint started")
    # Uvicorn does not automatically cancel handlers on client disconnect.
    task = asyncio.create_task(service.convert(file, request.state.request_id))
    try:
        while not task.done():
            if await request.is_disconnected():
                logger.warning("conversion client disconnected; cancelling work")
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise asyncio.CancelledError()
            await asyncio.wait({task}, timeout=0.1)
        result = await task
        job_id_context.set(result.job_id)
        logger.info("pdf download prepared job_id=%s bytes=%d", result.job_id, result.path.stat().st_size)
        return PipelinePdfResponse(result)
    except BaseException:
        if not task.done():
            task.cancel()
        try:
            result = await task
        except BaseException:
            pass
        raise
