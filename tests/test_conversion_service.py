import asyncio
import io
import json
import os
import time
from dataclasses import replace

import pytest
from starlette.datastructures import UploadFile

from app.config import Settings
from app.services.conversion_service import ConversionService


async def test_cancellation_releases_slot_and_keeps_original(tmp_path, docx_bytes):
    started = asyncio.Event()

    class BlockingConverter:
        async def convert_to_pdf(self, job_dir, suffix):
            started.set()
            await asyncio.Event().wait()

    service = ConversionService(replace(Settings(), work_root=tmp_path / "runtime", storage_root=tmp_path / "files"), BlockingConverter())
    service.prepare_storage()
    upload = UploadFile(io.BytesIO(docx_bytes), filename="private.docx")
    task = asyncio.create_task(service.convert(upload, "request"))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.active == 0
    assert not list((tmp_path / "runtime").iterdir())
    jobs = list((tmp_path / "files").iterdir())
    assert len(jobs) == 1
    assert (jobs[0] / "0" / "input.docx").read_bytes() == docx_bytes
    assert not (jobs[0] / "1").exists()
    assert json.loads((jobs[0] / "manifest.json").read_text())["status"] == "cancelled"
    assert upload.file.closed


def test_stale_cleanup_only_removes_owned_inactive_uuid_directories(tmp_path):
    service = ConversionService(replace(Settings(), work_root=tmp_path / "runtime", storage_root=tmp_path / "files", stale_job_seconds=1))
    service.prepare_storage()
    stale = service.settings.work_root / ("a" * 32)
    active = service.settings.work_root / ("b" * 32)
    unrelated = service.settings.work_root / "other-files"
    for folder in (stale, active, unrelated):
        folder.mkdir()
        os.utime(folder, (time.time() - 10, time.time() - 10))
    service.jobs.add(active)
    service.cleanup_stale()
    assert not stale.exists()
    assert active.exists() and unrelated.exists()


def test_remote_converter_endpoints_are_rejected():
    with pytest.raises(ValueError, match="local"):
        Settings(converter_url="http://remote.example:8001")
