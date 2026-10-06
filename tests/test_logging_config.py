import logging
import sys

from app.logging_config import LocalDiagnosticFormatter, configure_logging


def test_server_debug_configuration_is_idempotent(caplog):
    configure_logging()
    configure_logging()
    handlers = [handler for handler in logging.getLogger().handlers if getattr(handler, "mobidrill_console", False)]
    assert len(handlers) == 1
    assert handlers[0].level == logging.DEBUG
    for name in ("app", "main", "fastapi", "starlette", "uvicorn.error"):
        assert logging.getLogger(name).isEnabledFor(logging.DEBUG)
    logger = logging.getLogger("uvicorn.error")
    logger.debug("server debug verification")
    assert "server debug verification" in caplog.text
    assert logging.getLogger("uvicorn.access").disabled
    assert not logging.getLogger("pypdf").propagate


def test_exception_formatter_retains_type_and_location_without_message():
    private_content = "PRIVATE_DOCUMENT_CONTENT"
    try:
        raise ValueError(private_content)
    except ValueError:
        text = LocalDiagnosticFormatter().formatException(sys.exc_info())
    assert "ValueError" in text
    assert "test_exception_formatter" in text
    assert private_content not in text
