"""Run the local API against a private file without emitting document contents."""

import argparse
import asyncio
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from pypdf import PdfReader

from main import create_app


async def verify(path: Path):
    application = create_app()
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                     base_url="http://127.0.0.1") as client:
            with path.open("rb") as source:
                response = await client.post("/api/v1/conversions/pdf", files={"file": (path.name, source)})
    result = {"status_code": response.status_code}
    if "X-Job-ID" in response.headers:
        result["job_id"] = response.headers["X-Job-ID"]
    if response.status_code == 200:
        result.update(bytes=len(response.content), pages=len(PdfReader(io.BytesIO(response.content)).pages))
    else:
        result["error"] = response.json().get("error")
    print(json.dumps(result))
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    sys.exit(asyncio.run(verify(parser.parse_args().file)))
