"""Bound the body before multipart parsing and keep request identifiers server-generated."""

import asyncio
import ipaddress
import logging
import tempfile
import time
import uuid

from starlette.responses import JSONResponse

from app.exceptions import error_body
from app.logging_config import job_id_context, request_id_context, safe_exception_details

logger = logging.getLogger(__name__)


class LocalRequestMiddleware:
    def __init__(self, app, max_body_bytes: int, spool_directory: str):
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.spool_directory = spool_directory

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_context.set(request_id)
        job_token = job_id_context.set("-")
        started = time.monotonic()
        status, response_bytes = None, 0

        async def tracked_send(message):
            nonlocal status, response_bytes
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                response_bytes += len(message.get("body", b""))
            await send(message)

        logger.debug("http request received method=%s", scope["method"])
        try:
            return await self.handle_http(scope, receive, tracked_send, request_id)
        except asyncio.CancelledError:
            logger.warning("http request cancelled")
            raise
        except Exception:
            status = 500
            logger.error("http request failed stack=%s", safe_exception_details())
            raise
        finally:
            # Never log raw paths, query strings, headers, or multipart fields.
            route = scope.get("route")
            route_label = getattr(route, "path", None) or getattr(route, "name", None) or "unmatched"
            logger.info("http request finished method=%s route=%s status=%s response_bytes=%d elapsed_seconds=%.3f",
                        scope["method"], route_label, status or "disconnected", response_bytes,
                        time.monotonic() - started)
            request_id_context.reset(token)
            job_id_context.reset(job_token)

    async def handle_http(self, scope, receive, send, request_id):
        client = scope.get("client")
        if client is not None:
            try:
                is_local = ipaddress.ip_address(client[0]).is_loopback
            except ValueError:
                is_local = False
            if not is_local:
                logger.warning("request rejected code=LOCAL_ACCESS_ONLY status=403")
                response = JSONResponse(error_body("LOCAL_ACCESS_ONLY", "Only local clients are allowed.", request_id),
                                        status_code=403)
                return await response(scope, receive, send)
        if scope["path"] != "/api/v1/conversions/pdf" or scope["method"] != "POST":
            return await self.app(scope, receive, send)

        async def reject():
            logger.warning("upload rejected code=FILE_TOO_LARGE status=413 max_body_bytes=%d", self.max_body_bytes)
            response = JSONResponse(error_body("FILE_TOO_LARGE", f"Upload body exceeds the limit ({self.max_body_bytes} bytes).", request_id),
                                    status_code=413)
            await response(scope, receive, send)

        for key, value in scope.get("headers", []):
            if key.lower() == b"content-length":
                try:
                    if int(value) > self.max_body_bytes:
                        return await reject()
                except ValueError:
                    pass
        # Includes chunked bodies: never rely solely on Content-Length.
        logger.debug("upload body buffering started max_body_bytes=%d spool_memory_bytes=1048576", self.max_body_bytes)
        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, dir=self.spool_directory) as body:
            size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    logger.warning("upload client disconnected received_bytes=%d", size)
                    return
                data = message.get("body", b"")
                size += len(data)
                if size > self.max_body_bytes:
                    return await reject()
                await asyncio.to_thread(body.write, data)
                if not message.get("more_body", False):
                    break
            await asyncio.to_thread(body.seek, 0)
            logger.debug("upload body buffering completed received_bytes=%d", size)
            finished = False

            async def replay():
                nonlocal finished
                if finished:
                    return await receive()
                data = await asyncio.to_thread(body.read, 64 * 1024)
                finished = body.tell() == size
                return {"type": "http.request", "body": data, "more_body": not finished}

            await self.app(scope, replay, send)
