import asyncio
import json
import logging
import uuid
from zipfile import ZIP_STORED, ZipFile
from dataclasses import replace

import httpx
import pytest

from app.config import Settings
from app.exceptions import ConversionError
from main import create_app


class FakeConverter:
    def __init__(self, pdf, error=None, gate=None):
        self.pdf, self.error, self.gate = pdf, error, gate
        self.jobs = []
        self.started = asyncio.Event()

    async def convert_to_pdf(self, job_dir, suffix):
        self.jobs.append(job_dir)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error:
            raise self.error
        output = job_dir / "output"
        output.mkdir()
        path = output / "input.pdf"
        path.write_bytes(self.pdf)
        return path


def make_app(tmp_path, converter, **limits):
    return create_app(replace(Settings(), work_root=tmp_path / "runtime", storage_root=tmp_path / "files", **limits), converter)


def assert_runtime_clean(tmp_path):
    assert not list((tmp_path / "runtime").iterdir())


def assert_saved_job(tmp_path, response, source, suffix, pdf):
    job_id = response.headers["X-Job-ID"]
    assert str(uuid.UUID(job_id)) == job_id
    folder = tmp_path / "files" / job_id
    assert (folder / "0" / ("input" + suffix)).read_bytes() == source
    assert (folder / "1" / "input.pdf").read_bytes() == pdf
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["job_id"] == job_id
    assert manifest["status"] == "completed"
    assert [stage["index"] for stage in manifest["stages"]] == [0, 1]
    assert_runtime_clean(tmp_path)
    return folder


async def test_pdf_passthrough_and_persistent_stages(tmp_path, pdf_bytes):
    converter = FakeConverter(pdf_bytes)
    app = make_app(tmp_path, converter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        result = await client.post("/api/v1/conversions/pdf", files={"file": ("한글.pdf", pdf_bytes)})
        assert result.status_code == 200
        assert result.content == pdf_bytes
        assert result.headers["content-type"] == "application/pdf"
        assert result.headers["cache-control"] == "no-store"
        assert "filename*=" in result.headers["content-disposition"]
        assert not converter.jobs
        assert_saved_job(tmp_path, result, pdf_bytes, ".pdf", pdf_bytes)
        assert (await client.get("/")).json() == {"message": "Hello World"}
        assert (await client.get("/hello/User")).json() == {"message": "Hello User"}


async def test_docx_conversion_and_persistent_stages(tmp_path, pdf_bytes, docx_bytes):
    converter = FakeConverter(pdf_bytes)
    app = make_app(tmp_path, converter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        result = await client.post("/api/v1/conversions/pdf", files={"file": ("../../report.DOCX", docx_bytes)})
    assert result.status_code == 200
    assert result.content == pdf_bytes
    assert len(converter.jobs) == 1
    assert "report.pdf" in result.headers["content-disposition"]
    assert_saved_job(tmp_path, result, docx_bytes, ".docx", pdf_bytes)


async def test_default_limit_accepts_40_mib_file_and_keeps_original(tmp_path, pdf_bytes, docx_bytes):
    # Stream a genuinely large multipart upload through both body/file limits,
    # using a valid Office container and a fake converter (no real document data).
    source = tmp_path / "large.docx"
    source.write_bytes(docx_bytes)
    with ZipFile(source, "a", compression=ZIP_STORED) as archive:
        with archive.open("word/media/padding.bin", "w") as padding:
            chunk = b"x" * (1024 * 1024)
            for _ in range(40):
                padding.write(chunk)
    converter = FakeConverter(pdf_bytes)
    app = make_app(tmp_path, converter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        with source.open("rb") as upload:
            response = await client.post("/api/v1/conversions/pdf", files={"file": ("large.docx", upload)})
    assert response.status_code == 200, response.text
    assert len(converter.jobs) == 1
    folder = tmp_path / "files" / response.headers["X-Job-ID"]
    assert source.stat().st_size > 40 * 1024 * 1024
    assert (folder / "0" / "input.docx").stat().st_size == source.stat().st_size
    assert (folder / "1" / "input.pdf").read_bytes() == pdf_bytes
    assert_runtime_clean(tmp_path)


@pytest.mark.parametrize("status,code", [(504, "CONVERSION_TIMEOUT"), (503, "CONVERTER_UNAVAILABLE")])
async def test_failure_cleanup(tmp_path, pdf_bytes, docx_bytes, status, code):
    converter = FakeConverter(pdf_bytes, ConversionError(code, status, "Safe failure"))
    app = make_app(tmp_path, converter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        result = await client.post("/api/v1/conversions/pdf", files={"file": ("sensitive.docx", docx_bytes)})
    assert result.status_code == status
    assert result.json()["error"]["code"] == code
    assert "sensitive" not in result.text
    folder = tmp_path / "files" / result.headers["X-Job-ID"]
    assert (folder / "0" / "input.docx").read_bytes() == docx_bytes
    assert not (folder / "1").exists()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["error_code"] == code
    assert_runtime_clean(tmp_path)


async def test_missing_empty_and_oversize(tmp_path, pdf_bytes):
    app = make_app(tmp_path, FakeConverter(pdf_bytes), max_file_bytes=100)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        missing = await client.post("/api/v1/conversions/pdf")
        assert missing.status_code == 422
        empty = await client.post("/api/v1/conversions/pdf", files={"file": ("empty.docx", b"")})
        assert empty.json()["error"]["code"] == "EMPTY_FILE"
        large = await client.post("/api/v1/conversions/pdf", files={"file": ("large.pdf", b"x" * 101)})
        assert large.status_code == 413
        oversized_body = await client.post("/api/v1/conversions/pdf", content=b"x" * (1024 * 1024 + 101))
        assert oversized_body.status_code == 413
    assert_runtime_clean(tmp_path)
    assert not list((tmp_path / "files").iterdir())


async def test_chunked_body_limit(tmp_path, pdf_bytes):
    app = make_app(tmp_path, FakeConverter(pdf_bytes), max_file_bytes=1, multipart_overhead_bytes=10)

    async def chunks():
        yield b"x" * 5
        yield b"x" * 7

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        result = await client.post("/api/v1/conversions/pdf", content=chunks())
    assert result.status_code == 413
    assert_runtime_clean(tmp_path)
    assert not list((tmp_path / "files").iterdir())


async def test_concurrency_limit(tmp_path, pdf_bytes, docx_bytes):
    gate = asyncio.Event()
    converter = FakeConverter(pdf_bytes, gate=gate)
    app = make_app(tmp_path, converter, max_concurrent_conversions=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        first = asyncio.create_task(client.post("/api/v1/conversions/pdf", files={"file": ("same.docx", docx_bytes)}))
        await asyncio.wait_for(converter.started.wait(), 5)
        second = await client.post("/api/v1/conversions/pdf", files={"file": ("same.docx", docx_bytes)})
        assert second.status_code == 503
        assert second.json()["error"]["code"] == "CONVERTER_BUSY"
        gate.set()
        result = await first
        assert result.status_code == 200
        assert_saved_job(tmp_path, result, docx_bytes, ".docx", pdf_bytes)
    assert app.state.conversion_service.active == 0
    assert_runtime_clean(tmp_path)
    assert len(list((tmp_path / "files").iterdir())) == 1


async def test_invalid_converter_output(tmp_path, docx_bytes):
    app = make_app(tmp_path, FakeConverter(b"bad pdf"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        result = await client.post("/api/v1/conversions/pdf", files={"file": ("input.docx", docx_bytes)})
    assert result.status_code == 500
    folder = tmp_path / "files" / result.headers["X-Job-ID"]
    assert (folder / "0" / "input.docx").read_bytes() == docx_bytes
    assert not (folder / "1").exists()
    assert_runtime_clean(tmp_path)


async def test_remote_clients_are_rejected(tmp_path, pdf_bytes):
    app = make_app(tmp_path, FakeConverter(pdf_bytes))
    transport = httpx.ASGITransport(app=app, client=("192.0.2.123", 1000))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.post("/api/v1/conversions/pdf", files={"file": ("x.pdf", pdf_bytes)})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "LOCAL_ACCESS_ONLY"


async def test_docs_have_no_external_resources(tmp_path, pdf_bytes):
    app = make_app(tmp_path, FakeConverter(pdf_bytes))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get("/docs")
        schema = (await client.get("/openapi.json")).json()
    assert response.status_code == 200
    assert "http://" not in response.text and "https://" not in response.text
    assert 'enctype="multipart/form-data"' in response.text
    assert "ErrorResponse" in schema["components"]["schemas"]


async def test_swagger_ui_is_local_and_documents_pdf_contract(tmp_path, pdf_bytes):
    app = make_app(tmp_path, FakeConverter(pdf_bytes))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get("/swagger-ui")
        assert response.status_code == 200
        assert "http://" not in response.text and "https://" not in response.text
        assert '"validatorUrl": "none"' in response.text
        assert '"queryConfigEnabled": false' in response.text
        assert "connect-src 'self'" in response.headers["content-security-policy"]
        assert 'script nonce="' in response.text
        for asset in ("swagger-ui-bundle.js", "swagger-ui.css", "favicon.svg"):
            assert "/static/swagger-ui/" + asset in response.text
            resource = await client.get("/static/swagger-ui/" + asset)
            assert resource.status_code == 200
            assert resource.content
        schema = (await client.get("/openapi.json")).json()
    operation = schema["paths"]["/api/v1/conversions/pdf"]["post"]
    assert operation["operationId"] == "convertDocumentToPdf"
    assert operation["requestBody"]["required"] is True
    assert "multipart/form-data" in operation["requestBody"]["content"]
    assert operation["responses"]["200"]["content"]["application/pdf"]["schema"]["format"] == "binary"
    assert operation["responses"]["200"]["headers"]["X-Job-ID"]["schema"]["format"] == "uuid"
    for status in (400, 403, 413, 415, 422, 500, 503, 504):
        assert operation["responses"][str(status)]["content"]["application/json"]["schema"]["$ref"].endswith("ErrorResponse")


async def test_same_filename_is_isolated_and_files_are_not_public(tmp_path, pdf_bytes):
    app = make_app(tmp_path, FakeConverter(pdf_bytes))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        first = await client.post("/api/v1/conversions/pdf", files={"file": ("same.pdf", pdf_bytes)})
        second = await client.post("/api/v1/conversions/pdf", files={"file": ("same.pdf", pdf_bytes)})
        assert first.headers["X-Job-ID"] != second.headers["X-Job-ID"]
        first_folder = assert_saved_job(tmp_path, first, pdf_bytes, ".pdf", pdf_bytes)
        assert_saved_job(tmp_path, second, pdf_bytes, ".pdf", pdf_bytes)
        for prefix in ("/resources/static", "/static"):
            response = await client.get(prefix + "/" + first_folder.name + "/0/input.pdf")
            assert response.status_code == 404


async def test_completed_pipeline_survives_startup_and_future_stages(tmp_path, pdf_bytes):
    app = make_app(tmp_path, FakeConverter(pdf_bytes), stale_job_seconds=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post("/api/v1/conversions/pdf", files={"file": ("file.pdf", pdf_bytes)})
    folder = assert_saved_job(tmp_path, response, pdf_bytes, ".pdf", pdf_bytes)
    stage = app.state.conversion_service.storage.stage_directory(folder.name, 2)
    stage.mkdir()
    (stage / "result.txt").write_text("future pipeline output")
    import os
    import time
    os.utime(folder, (time.time() - 3600, time.time() - 3600))
    restarted = make_app(tmp_path, FakeConverter(pdf_bytes), stale_job_seconds=1)
    async with restarted.router.lifespan_context(restarted):
        restarted.state.conversion_service.cleanup_stale()
    assert (folder / "0" / "input.pdf").read_bytes() == pdf_bytes
    assert (folder / "1" / "input.pdf").read_bytes() == pdf_bytes
    assert (stage / "result.txt").read_text() == "future pipeline output"


async def test_debug_logs_trace_stages_without_document_data(tmp_path, pdf_bytes, docx_bytes, caplog):
    caplog.set_level(logging.DEBUG, logger="app")
    app = make_app(tmp_path, FakeConverter(pdf_bytes))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post("/api/v1/conversions/pdf?token=PRIVATE_QUERY",
                                     files={"file": ("PRIVATE_FILENAME.docx", docx_bytes)})
        await client.get("/hello/PRIVATE_PATH?token=PRIVATE_QUERY")
    assert response.status_code == 200
    assert "PRIVATE_FILENAME" not in caplog.text
    assert "PRIVATE_QUERY" not in caplog.text
    assert "PRIVATE_PATH" not in caplog.text
    assert "route=/hello/{name}" in caplog.text
    for step in ("body buffering completed", "source storage completed", "validation completed",
                 "pdf conversion started", "pdf conversion completed", "pipeline pdf saved",
                 "pdf download prepared", "workspace cleanup completed", "status=200"):
        assert step in caplog.text
    stages = [record for record in caplog.records if "source storage" in record.getMessage()]
    assert stages and all(record.levelno == logging.DEBUG for record in stages)
    assert all(record.job_id == response.headers["X-Job-ID"] for record in stages)
    assert len({record.request_id for record in stages}) == 1
    assert stages[0].request_id != "-"


async def test_debug_failure_logs_omit_exception_message(tmp_path, pdf_bytes, docx_bytes, caplog):
    caplog.set_level(logging.DEBUG, logger="app")
    app = make_app(tmp_path, FakeConverter(pdf_bytes, RuntimeError("PRIVATE_DOCUMENT_CONTENT")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post("/api/v1/conversions/pdf", files={"file": ("PRIVATE_NAME.docx", docx_bytes)})
    assert response.status_code == 500
    assert "PRIVATE_DOCUMENT_CONTENT" not in caplog.text
    assert "PRIVATE_NAME" not in caplog.text
    assert "RuntimeError" in caplog.text
    assert "code=CONVERSION_FAILED" in caplog.text
    assert "source_retained=True" in caplog.text


async def test_upload_rejection_logs_actual_body_limit(tmp_path, pdf_bytes, caplog):
    app = make_app(tmp_path, FakeConverter(pdf_bytes), max_file_bytes=10, multipart_overhead_bytes=10)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post("/api/v1/conversions/pdf", content=b"x" * 21)
    assert response.status_code == 413
    assert "max_body_bytes=20" in caplog.text
    assert "status=413" in caplog.text
