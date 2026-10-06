"""Local console diagnostics without document data or request parameters."""

import logging
import sys
import traceback
from contextvars import ContextVar

request_id_context = ContextVar("request_id", default="-")
job_id_context = ContextVar("job_id", default="-")


def safe_exception_details():
    exception_type, _, stack = sys.exc_info()
    return format_safe_exception(exception_type, stack)


def format_safe_exception(exception_type, stack):
    frames = traceback.extract_tb(stack)
    return "".join(f"  File {frame.filename}, line {frame.lineno}, in {frame.name}\n" for frame in frames) + exception_type.__name__


class RequestContextFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_context.get()
        record.job_id = job_id_context.get()
        return True


class LocalDiagnosticFormatter(logging.Formatter):
    def formatException(self, exc_info):
        # Exception messages can contain parser input. Retain stack locations
        # and the exception type, without printing document data from messages.
        exception_type, _, stack = exc_info
        return format_safe_exception(exception_type, stack)


def configure_logging():
    root = logging.getLogger()
    handler = next((item for item in root.handlers if getattr(item, "mobidrill_console", False)), None)
    if handler is None:
        handler = logging.StreamHandler(sys.stderr)
        handler.mobidrill_console = True
        handler.setLevel(logging.DEBUG)
        handler.addFilter(RequestContextFilter())
        handler.setFormatter(LocalDiagnosticFormatter(
            "%(asctime)s %(levelname)-8s %(name)s request_id=%(request_id)s job_id=%(job_id)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in ("app", "main", "fastapi", "starlette", "uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        if name.startswith("uvicorn"):
            logger.handlers.clear()
            logger.propagate = True
    # Middleware emits access logs using route templates. Uvicorn's access
    # logger prints raw URLs and query strings supplied by clients.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("pypdf", "olefile", "python_multipart", "multipart"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
