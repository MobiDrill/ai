"""Persistent local HTTP conversion service; no Docker client or remote calls."""

import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROFILE_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
 <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
  <prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
  <prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>
 </item>
 <item oor:path="/org.openoffice.Office.Writer/Content/Update">
  <prop oor:name="Link" oor:op="fuse"><value>0</value></prop>
 </item>
</oor:items>'''
logger = logging.getLogger("libreoffice.worker")


class WorkerError(Exception):
    def __init__(self, code, status):
        self.code, self.status = code, status


def convert(root, suffix, timeout, max_output_bytes):
    output, profile = root / "output", root / "profile"
    output.mkdir(mode=0o700)
    (profile / "user").mkdir(parents=True, mode=0o700)
    (profile / "user" / "registrymodifications.xcu").write_text(PROFILE_XML, encoding="utf-8")
    pdf_filter = "impress_pdf_Export" if suffix in {".ppt", ".pptx"} else "writer_pdf_Export"
    command = [sys.executable, str(Path(__file__).with_name("lo_process.py")), "/usr/bin/libreoffice",
               "--headless", "--nologo", "--nodefault", "--norestore",
               "-env:UserInstallation=" + profile.as_uri(), "--convert-to", "pdf:" + pdf_filter,
               "--outdir", str(output), str(root / ("input" + suffix))]
    process = None
    try:
        process = subprocess.Popen(command, env={"PATH": "/usr/bin:/bin", "HOME": str(root),
                                   "TMPDIR": str(root), "LANG": "C.UTF-8", "SAL_USE_VCLPLUGIN": "svp"},
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise WorkerError("CONVERSION_TIMEOUT", 504) from None
        logger.debug("libreoffice exited exit_code=%d", exit_code)
        if exit_code == 120:
            raise WorkerError("CONVERTER_UNAVAILABLE", 503)
        if exit_code in (-9, 137):
            raise WorkerError("CONVERTER_RESOURCE_LIMIT", 503)
        target = output / "input.pdf"
        if exit_code or target.is_symlink() or not target.is_file() or not 0 < target.stat().st_size <= max_output_bytes:
            raise WorkerError("CONVERSION_FAILED", 500)
        with target.open("rb") as pdf:
            if pdf.read(5) != b"%PDF-":
                raise WorkerError("CONVERSION_FAILED", 500)
        return target
    except OSError:
        raise WorkerError("CONVERTER_UNAVAILABLE", 503) from None
    finally:
        if process is not None:
            # Terminate the entire LibreOffice group, including surviving child
            # processes, before deleting the request's private workspace.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


class ConversionServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, **limits):
        super().__init__(address, Handler)
        self.limits = limits
        self.slots = threading.BoundedSemaphore(limits["concurrency"])
        self.network_blocked = False

    def handle_error(self, request, client_address):
        logger.error("worker request failed exception_type=%s", sys.exc_info()[0].__name__)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Raw URLs, headers, and document parameters must not be logged.

    def send_json(self, status, data):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            ready = Path("/usr/bin/libreoffice").is_file() and self.server.network_blocked
            self.send_json(200 if ready else 503, {"status": "ready" if ready else "unavailable",
                                                 "network_blocked": self.server.network_blocked})
        else:
            self.send_json(404, {"error": {"code": "NOT_FOUND"}})

    def do_POST(self):
        started = time.monotonic()
        request_id = self.headers.get("X-Request-ID", "")
        request_id = request_id if re.fullmatch(r"[a-f0-9]{32}", request_id) else "-"
        acquired = False
        outcome = "CONVERSION_FAILED"
        try:
            match = re.fullmatch(r"/convert/(\.docx|\.ppt|\.pptx|\.hwp)", self.path)
            if match is None:
                raise WorkerError("UNSUPPORTED_FORMAT", 415)
            suffix = match.group(1)
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type") != "application/octet-stream":
                raise WorkerError("INVALID_DOCUMENT", 400)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise WorkerError("INVALID_DOCUMENT", 400) from None
            if length <= 0:
                raise WorkerError("INVALID_DOCUMENT", 400)
            if length > self.server.limits["max_file_bytes"]:
                raise WorkerError("FILE_TOO_LARGE", 413)
            acquired = self.server.slots.acquire(blocking=False)
            if not acquired:
                raise WorkerError("CONVERTER_BUSY", 503)
            self.connection.settimeout(30)
            with tempfile.TemporaryDirectory(prefix="conversion-", dir="/tmp") as folder:
                root = Path(folder)
                remaining = length
                with (root / ("input" + suffix)).open("wb") as document:
                    while remaining:
                        chunk = self.rfile.read(min(64 * 1024, remaining))
                        if not chunk:
                            raise WorkerError("INVALID_DOCUMENT", 400)
                        document.write(chunk)
                        remaining -= len(chunk)
                logger.debug("worker upload completed request_id=%s format=%s bytes=%d", request_id, suffix, length)
                target = convert(root, suffix, self.server.limits["timeout"], self.server.limits["max_output_bytes"])
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(target.stat().st_size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with target.open("rb") as pdf:
                    while chunk := pdf.read(64 * 1024):
                        self.wfile.write(chunk)
                outcome = "success"
        except WorkerError as exc:
            outcome = exc.code
            self.send_json(exc.status, {"error": {"code": exc.code}})
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            outcome = "CLIENT_DISCONNECTED"
        except Exception as exc:
            logger.error("worker unexpected failure request_id=%s exception_type=%s", request_id, type(exc).__name__)
            self.send_json(500, {"error": {"code": "CONVERSION_FAILED"}})
        finally:
            if acquired:
                self.server.slots.release()
            logger.info("worker request finished request_id=%s result=%s elapsed_seconds=%.3f",
                        request_id, outcome, time.monotonic() - started)


def main():
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    limits = {"concurrency": int(os.getenv("MAX_CONCURRENT_CONVERSIONS", "2")),
              "timeout": int(os.getenv("CONVERSION_TIMEOUT_SECONDS", "60")),
              "max_file_bytes": int(os.getenv("MAX_FILE_BYTES", str(100 * 1024 * 1024))),
              "max_output_bytes": int(os.getenv("MAX_OUTPUT_BYTES", str(100 * 1024 * 1024)))}
    if any(value <= 0 for value in limits.values()) or limits["timeout"] > 3600:
        raise ValueError("Invalid worker limits")
    logger.info("worker started port=8001 concurrency=%d timeout_seconds=%d", limits["concurrency"], limits["timeout"])
    with ConversionServer(("0.0.0.0", 8001), **limits) as server:
        from lo_process import block_network
        # Bind the inbound listener first, then forbid creating any new IP
        # sockets. Accepted inbound connections still work. Every subsequently
        # created thread and LibreOffice child inherits this seccomp filter.
        block_network()
        for family in (socket.AF_INET, socket.AF_INET6):
            try:
                connection = socket.socket(family)
            except PermissionError:
                continue
            connection.close()
            raise RuntimeError("Worker network sandbox is not enforced")
        server.network_blocked = True
        server.serve_forever()


if __name__ == "__main__":
    main()
