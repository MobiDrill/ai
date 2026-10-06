import io
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from pypdf import PdfReader

from app.config import Settings
from main import create_app

pytestmark = [pytest.mark.integration, pytest.mark.skipif(os.getenv("RUN_OFFLINE_INTEGRATION") != "1",
              reason="Requires the persistent local LibreOffice service started with docker compose")]


@pytest.mark.parametrize("fixture_name,suffix", [("docx_bytes", ".docx"), ("pptx_bytes", ".pptx")])
async def test_real_http_conversion(tmp_path, request, fixture_name, suffix):
    source = request.getfixturevalue(fixture_name)
    app = create_app(replace(Settings.from_env(), work_root=tmp_path / "runtime", storage_root=tmp_path / "files"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.post("/api/v1/conversions/pdf", files={"file": ("input" + suffix, source)})
    assert response.status_code == 200, response.text
    assert len(PdfReader(io.BytesIO(response.content)).pages) == 1
    folder = tmp_path / "files" / response.headers["X-Job-ID"]
    assert (folder / "0" / ("input" + suffix)).read_bytes() == source
    assert (folder / "1" / "input.pdf").read_bytes() == response.content
    assert not list((tmp_path / "runtime").iterdir())


def test_libreoffice_process_cannot_create_network_sockets():
    # Deployment verification only. Application code never invokes Docker.
    script = """import socket
for family in (socket.AF_INET, socket.AF_INET6):
    try:
        socket.socket(family)
    except PermissionError:
        pass
    else:
        raise AssertionError('Network socket was allowed')
socket.socket(socket.AF_UNIX).close()
"""
    result = subprocess.run(["docker", "compose", "exec", "-T", "libreoffice", "/usr/bin/python3",
                             "/opt/converter/lo_process.py", "/usr/bin/python3", "-c", script],
                            cwd=Path(__file__).resolve().parents[2], timeout=15,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert result.returncode == 0


async def test_worker_startup_verifies_network_sandbox():
    async with httpx.AsyncClient(trust_env=False) as client:
        result = await client.get(Settings.from_env().converter_url + "/health")
    assert result.status_code == 200
    assert result.json() == {"status": "ready", "network_blocked": True}
