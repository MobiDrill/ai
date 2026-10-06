import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from app.logging_config import job_id_context

logger = logging.getLogger(__name__)


class ConversionError(Exception):
    def __init__(self, code: str, status: int, message: str):
        super().__init__(code)
        self.code, self.status, self.message = code, status, message
        self.job_id: str | None = None


def error_body(code: str, message: str, request_id: str):
    return {"error": {"code": code, "message": message, "request_id": request_id}}


async def conversion_error_handler(request: Request, exc: ConversionError):
    if exc.job_id is not None:
        job_id_context.set(exc.job_id)
    logger.warning("api error response code=%s status=%d job_id=%s", exc.code, exc.status, exc.job_id or "-")
    headers = {"Retry-After": "5"} if exc.code == "CONVERTER_BUSY" else {}
    if exc.job_id is not None:
        headers["X-Job-ID"] = exc.job_id
    return JSONResponse(error_body(exc.code, exc.message, request.state.request_id),
                        status_code=exc.status, headers=headers)
