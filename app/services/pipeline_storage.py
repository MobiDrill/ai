"""Persistent, numbered pipeline stages. Never served as HTTP static files."""

import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineJob:
    job_id: str
    directory: Path
    source_path: Path
    pdf_path: Path
    original_filename: str


class PipelineStorage:
    def __init__(self, root: Path):
        self.root = root

    def prepare(self):
        if self.root.is_symlink():
            raise ValueError("Pipeline storage must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.root.chmod(0o700)

    def create_job(self, suffix: str, original_filename: str):
        if not re.fullmatch(r"\.[a-z0-9]+", suffix):
            raise ValueError("Invalid source extension")
        job_id = str(uuid.uuid4())
        directory = self.root / job_id
        directory.mkdir(mode=0o700)
        (directory / "0").mkdir(mode=0o700)
        logger.debug("pipeline job created job_id=%s stage=0 format=%s", job_id, suffix)
        return PipelineJob(job_id, directory, directory / "0" / ("input" + suffix),
                           directory / "1" / "input.pdf", original_filename)

    def stage_directory(self, job_id: str, index: int):
        """Return a confined stage path for future pipeline steps 2, 3, ..."""
        if str(uuid.UUID(job_id)) != job_id or type(index) is not int or index < 0:
            raise ValueError("Invalid pipeline job or stage")
        directory = self.root / job_id
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Unknown pipeline job")
        target = directory / str(index)
        if target.is_symlink() or not target.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("Stage path escapes storage")
        return target

    def _check_job(self, job: PipelineJob):
        if job.directory.is_symlink() or job.directory.resolve().parent != self.root.resolve():
            raise ValueError("Pipeline job escapes storage")

    def write_manifest(self, job: PipelineJob, status: str, error_code: str | None = None):
        self._check_job(job)
        stages = [{"index": 0, "operation": "upload", "status": "completed",
                   "path": job.source_path.relative_to(job.directory).as_posix()}]
        if status != "source_ready":
            stages.append({"index": 1, "operation": "pdf_conversion", "status": status,
                           "path": "1/input.pdf" if status == "completed" else None})
        data = {"job_id": job.job_id, "original_filename": job.original_filename,
                "status": status, "error_code": error_code, "stages": stages}
        temporary = job.directory / ".manifest.json.tmp"
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(job.directory / "manifest.json")
        logger.debug("pipeline manifest saved job_id=%s status=%s error_code=%s", job.job_id, status, error_code or "-")

    def save_pdf(self, job: PipelineJob, source: Path):
        self._check_job(job)
        stage = self.stage_directory(job.job_id, 1)
        stage.mkdir(mode=0o700)
        temporary = stage / ".input.pdf.partial"
        shutil.copyfile(source, temporary)
        temporary.replace(job.pdf_path)
        logger.info("pipeline pdf saved job_id=%s stage=1 bytes=%d", job.job_id, job.pdf_path.stat().st_size)

    def mark_failed(self, job: PipelineJob, code: str, cancelled: bool = False):
        self._check_job(job)
        stage = self.stage_directory(job.job_id, 1)
        if stage.exists():
            shutil.rmtree(stage)
        self.write_manifest(job, "cancelled" if cancelled else "failed", code)
        logger.debug("pipeline failed job retained job_id=%s source_stage=0", job.job_id)

    def discard_invalid(self, job: PipelineJob):
        self._check_job(job)
        shutil.rmtree(job.directory, ignore_errors=True)
        logger.debug("invalid pipeline job discarded job_id=%s remaining=%s", job.job_id, job.directory.exists())
