import asyncio
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile

from app.config import Settings
from app.exceptions import ConversionError
from app.logging_config import job_id_context, safe_exception_details
from app.services.document_validator import DocumentValidator
from app.services.libreoffice_converter import LibreOfficeConverter
from app.services.pipeline_storage import PipelineStorage

logger = logging.getLogger(__name__)


@dataclass
class ConversionResult:
    path: Path
    filename: str
    job_dir: Path
    job_id: str


class ConversionService:
    def __init__(self, settings: Settings, converter=None):
        self.settings = settings
        self.converter = converter or LibreOfficeConverter(settings)
        self.validator = DocumentValidator(settings)
        self.storage = PipelineStorage(settings.storage_root)
        self.active = 0
        self.jobs: set[Path] = set()

    def prepare_storage(self):
        if self.settings.work_root.is_symlink():
            raise ValueError("Work root must not be a symlink")
        self.settings.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # chmod does not implement private ACLs on Windows; use a private
        # account-owned folder and configure Windows ACLs during deployment.
        if os.name != "nt":
            self.settings.work_root.chmod(0o700)
        self.storage.prepare()
        logger.debug("storage prepared temporary_and_pipeline_roots_ready=true")

    def cleanup(self, job_dir: Path):
        # Only directories created by this service may be deleted.
        if job_dir in self.jobs:
            shutil.rmtree(job_dir, ignore_errors=True)
            self.jobs.discard(job_dir)
            logger.debug("temporary workspace cleanup completed remaining=%s", job_dir.exists())

    def cleanup_stale(self):
        cutoff = time.time() - self.settings.stale_job_seconds
        removed = 0
        for entry in self.settings.work_root.iterdir():
            if entry in self.jobs or entry.is_symlink() or not entry.is_dir():
                continue
            if len(entry.name) != 32 or any(c not in "0123456789abcdef" for c in entry.name):
                continue
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += int(not entry.exists())
        logger.debug("stale workspace cleanup completed removed=%d active=%d", removed, len(self.jobs))

    async def convert(self, upload: UploadFile, request_id: str):
        original = (upload.filename or "document").replace("\\", "/").split("/")[-1]
        suffix = Path(original).suffix.lower()
        if suffix not in self.validator.EXTENSIONS:
            logger.warning("conversion rejected request_id=%s code=UNSUPPORTED_FORMAT", request_id)
            await upload.close()
            raise ConversionError("UNSUPPORTED_FORMAT", 415, "Unsupported document format.")
        # No await between checking and reserving a slot: atomic in this event loop.
        if self.active >= self.settings.max_concurrent_conversions:
            logger.warning("conversion rejected request_id=%s code=CONVERTER_BUSY active=%d limit=%d",
                           request_id, self.active, self.settings.max_concurrent_conversions)
            await upload.close()
            raise ConversionError("CONVERTER_BUSY", 503, "Conversion capacity is currently full.")
        self.active += 1
        job_dir = self.settings.work_root / uuid.uuid4().hex
        started, size = time.monotonic(), 0
        success = False
        source_ready = False
        pipeline_job = None
        error_code = None
        job_token = None
        logger.info("conversion started request_id=%s format=%s active=%d max_file_bytes=%d",
                    request_id, suffix, self.active, self.settings.max_file_bytes)
        try:
            job_dir.mkdir(mode=0o700)
            self.jobs.add(job_dir)
            original_name = "".join(c for c in original if c.isprintable())[:255]
            pipeline_job = self.storage.create_job(suffix, original_name)
            job_token = job_id_context.set(pipeline_job.job_id)
            logger.debug("upload source storage started stage=0")
            input_path = pipeline_job.source_path
            with input_path.open("wb") as destination:
                while chunk := await upload.read(64 * 1024):
                    size += len(chunk)
                    if size > self.settings.max_file_bytes:
                        raise ConversionError("FILE_TOO_LARGE", 413,
                                              f"The uploaded file exceeds the limit ({self.settings.max_file_bytes} bytes).")
                    destination.write(chunk)
            logger.debug("upload source storage completed stage=0 bytes=%d", size)
            # Parse outside the ASGI event loop. Await completion even if cancelled
            # so cleanup cannot race a parser still reading its input.
            await self._validate(input_path, output=False)
            self.storage.write_manifest(pipeline_job, "source_ready")
            source_ready = True
            if suffix == ".pdf":
                logger.debug("pdf passthrough selected")
                output_path = input_path
            else:
                # Keep the persistent source immutable to the conversion worker.
                await self._run_io(shutil.copyfile, input_path, job_dir / ("input" + suffix))
                logger.debug("converter workspace input copy completed bytes=%d", size)
                logger.info("pdf conversion started stage=1 format=%s", suffix)
                output_path = await self.converter.convert_to_pdf(job_dir, suffix)
                logger.info("pdf conversion completed stage=1")
            if suffix != ".pdf":
                await self._validate(output_path, output=True)
            await self._run_io(self.storage.save_pdf, pipeline_job, output_path)
            self.storage.write_manifest(pipeline_job, "completed")
            stem = "".join(c for c in Path(original).stem if c.isprintable() and c not in '/\\";')[:120] or "document"
            success = True
            return ConversionResult(pipeline_job.pdf_path, stem + ".pdf", pipeline_job.directory,
                                    pipeline_job.job_id)
        except ConversionError as exc:
            error_code = exc.code
            logger.warning("conversion failed code=%s status=%d source_retained=%s", exc.code, exc.status, source_ready)
            if source_ready:
                exc.job_id = pipeline_job.job_id
            raise
        except asyncio.CancelledError:
            error_code = "CANCELLED"
            logger.warning("conversion cancelled source_retained=%s", source_ready)
            raise
        except Exception:
            error_code = "CONVERSION_FAILED"
            logger.error("conversion unexpected failure code=CONVERSION_FAILED source_retained=%s stack=%s",
                         source_ready, safe_exception_details())
            exception = ConversionError(error_code, 500, "Document conversion failed.")
            if source_ready:
                exception.job_id = pipeline_job.job_id
            raise exception from None
        finally:
            self.active -= 1
            try:
                await upload.close()
            finally:
                self.cleanup(job_dir)
                if not success and pipeline_job is not None:
                    if source_ready:
                        self.storage.mark_failed(pipeline_job, error_code or "CONVERSION_FAILED",
                                                 cancelled=error_code == "CANCELLED")
                    else:
                        self.storage.discard_invalid(pipeline_job)
            logger.info("conversion request_id=%s format=%s bytes=%s elapsed=%.3f result=%s",
                        request_id, suffix, size, time.monotonic() - started, error_code or "success")
            if job_token is not None:
                job_id_context.reset(job_token)

    async def _validate(self, path: Path, output: bool):
        started = time.monotonic()
        logger.debug("document validation started kind=%s", "output_pdf" if output else "input")
        function = self.validator.validate_pdf if output else self.validator.validate_input
        args = (path, True) if output else (path,)
        await self._run_io(function, *args)
        logger.debug("document validation completed kind=%s elapsed_seconds=%.3f",
                     "output_pdf" if output else "input", time.monotonic() - started)

    async def _run_io(self, function, *args):
        # Wait for local reads/copies to finish before cancellation cleanup.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception:
                pass
            raise
