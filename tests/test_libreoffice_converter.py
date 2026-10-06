import asyncio
from dataclasses import replace

import httpx
import pytest

from app.config import Settings
from app.exceptions import ConversionError
from app.services.libreoffice_converter import LibreOfficeConverter


def make_converter(tmp_path, handler, **limits):
    (tmp_path / "input.docx").write_bytes(b"local document bytes")
    return LibreOfficeConverter(replace(Settings(), **limits), httpx.MockTransport(handler))


async def test_http_conversion_sends_bytes_and_saves_pdf(tmp_path):
    async def handle(request):
        assert str(request.url) == "http://127.0.0.1:8001/convert/.docx"
        assert await request.aread() == b"local document bytes"
        assert request.headers["content-length"] == "20"
        assert request.headers["content-type"] == "application/octet-stream"
        return httpx.Response(200, content=b"%PDF-local", headers={"Content-Type": "application/pdf"})

    converter = make_converter(tmp_path, handle)
    result = await converter.convert_to_pdf(tmp_path, ".docx")
    assert result.read_bytes() == b"%PDF-local"


@pytest.mark.parametrize("code,status", [("CONVERSION_TIMEOUT", 504), ("CONVERTER_BUSY", 503),
                                      ("CONVERTER_RESOURCE_LIMIT", 503), ("CONVERSION_FAILED", 500)])
async def test_worker_errors_are_mapped_without_raw_messages(tmp_path, code, status):
    converter = make_converter(tmp_path, lambda _: httpx.Response(status, json={"error": {"code": code, "message": "PRIVATE"}}))
    with pytest.raises(ConversionError) as error:
        await converter.convert_to_pdf(tmp_path, ".docx")
    assert error.value.code == code and error.value.status == status
    assert "PRIVATE" not in error.value.message


async def test_worker_unavailable_and_timeouts(tmp_path):
    async def handle(request):
        raise httpx.ConnectError("PRIVATE", request=request)

    converter = make_converter(tmp_path, handle)
    with pytest.raises(ConversionError, match="CONVERTER_UNAVAILABLE"):
        await converter.convert_to_pdf(tmp_path, ".docx")


@pytest.mark.parametrize("response", [httpx.Response(302, headers={"Location": "https://remote.example"}),
                                    httpx.Response(200, content=b"bad", headers={"Content-Type": "text/html"}),
                                    httpx.Response(500, content=b"x" * 4097),
                                    httpx.Response(500, json={"error": {"code": "PRIVATE_UNKNOWN"}})])
async def test_invalid_responses_and_redirects_fail_closed(tmp_path, response):
    converter = make_converter(tmp_path, lambda _: response)
    with pytest.raises(ConversionError, match="CONVERSION_FAILED"):
        await converter.convert_to_pdf(tmp_path, ".docx")


async def test_output_limit(tmp_path):
    converter = make_converter(tmp_path, lambda _: httpx.Response(200, content=b"%PDF-too-large",
                               headers={"Content-Type": "application/pdf"}), max_output_bytes=5)
    with pytest.raises(ConversionError, match="CONVERSION_FAILED"):
        await converter.convert_to_pdf(tmp_path, ".docx")


@pytest.mark.parametrize("url", ["http://example.com:8001", "http://127.0.0.1:8001/?url=remote",
                               "http://localhost:8001", "http://127.0.0.1:8001/remote", "https://127.0.0.1:8001",
                               "http://user:password@127.0.0.1:8001", "http://127.0.0.1"])
def test_nonlocal_or_ambiguous_worker_urls_are_rejected(url):
    with pytest.raises(ValueError, match="local"):
        Settings(converter_url=url)
