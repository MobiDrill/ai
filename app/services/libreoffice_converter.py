"""Stream documents to the persistent local LibreOffice HTTP worker."""

import json
import logging
import time
from pathlib import Path

import httpx

from app.config import Settings
from app.exceptions import ConversionError
from app.logging_config import request_id_context

logger = logging.getLogger(__name__)
WORKER_ERRORS = {
    "CONVERSION_FAILED": (500, "Document conversion failed."),
    "CONVERSION_TIMEOUT": (504, "Document conversion timed out."),
    "CONVERTER_UNAVAILABLE": (503, "Local conversion server is unavailable."),
    "CONVERTER_BUSY": (503, "Conversion capacity is currently full."),
    "CONVERTER_RESOURCE_LIMIT": (503, "Conversion exceeded the configured resource limit."),
    "FILE_TOO_LARGE": (413, "Local conversion server upload limit exceeded."),
    "INVALID_DOCUMENT": (400, "Invalid or damaged document."),
    "UNSUPPORTED_FORMAT": (415, "Unsupported document format."),
}


class LibreOfficeConverter:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.transport = transport

    async def convert_to_pdf(self, job_dir: Path, suffix: str):
        if suffix not in {".docx", ".ppt", ".pptx", ".hwp"}:
            raise ConversionError("UNSUPPORTED_FORMAT", 415, "Unsupported document format.")
        source = job_dir / ("input" + suffix)
        output = job_dir / "output"
        output.mkdir(mode=0o700)
        target = output / "input.pdf"
        started = time.monotonic()
        size = 0
        try:
            with source.open("rb") as document:
                async def chunks():
                    while chunk := document.read(64 * 1024):
                        yield chunk

                logger.debug("converter http request started format=%s bytes=%d timeout_seconds=%d",
                             suffix, source.stat().st_size, self.settings.conversion_timeout_seconds)
                async with httpx.AsyncClient(base_url=self.settings.converter_url, transport=self.transport,
                                             trust_env=False, follow_redirects=False,
                                             timeout=httpx.Timeout(self.settings.conversion_timeout_seconds + 10, connect=5)) as client:
                    async with client.stream("POST", "/convert/" + suffix, content=chunks(), headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(source.stat().st_size),
                        "X-Request-ID": request_id_context.get(),
                    }) as response:
                        logger.debug("converter http response status=%d", response.status_code)
                        if response.status_code != 200:
                            error = bytearray()
                            async for chunk in response.aiter_bytes():
                                error.extend(chunk)
                                if len(error) > 4096:
                                    raise ValueError("Oversized worker error")
                            code = json.loads(error).get("error", {}).get("code")
                            if not isinstance(code, str) or code not in WORKER_ERRORS:
                                raise ValueError("Invalid worker error")
                            status, message = WORKER_ERRORS[code]
                            raise ConversionError(code, status, message)
                        if response.headers.get("content-type", "").split(";")[0] != "application/pdf":
                            raise ValueError("Invalid worker media type")
                        with target.open("wb") as destination:
                            async for chunk in response.aiter_bytes():
                                size += len(chunk)
                                if size > self.settings.max_output_bytes:
                                    raise ValueError("Worker PDF exceeds output limit")
                                destination.write(chunk)
            if not size:
                raise ValueError("Empty worker PDF")
            logger.info("converter http completed bytes=%d elapsed_seconds=%.3f", size, time.monotonic() - started)
            return target
        except httpx.TimeoutException:
            logger.warning("converter http timeout")
            raise ConversionError("CONVERSION_TIMEOUT", 504, "Local conversion server timed out.") from None
        except httpx.RequestError as exc:
            logger.error("converter http unavailable exception_type=%s", type(exc).__name__)
            raise ConversionError("CONVERTER_UNAVAILABLE", 503, "Local conversion server is unavailable; start docker compose.") from None
        except (ValueError, TypeError, AttributeError) as exc:
            logger.error("converter http invalid response exception_type=%s", type(exc).__name__)
            raise ConversionError("CONVERSION_FAILED", 500, "Invalid conversion server response.") from None
        finally:
            logger.debug("converter http request finished elapsed_seconds=%.3f", time.monotonic() - started)
