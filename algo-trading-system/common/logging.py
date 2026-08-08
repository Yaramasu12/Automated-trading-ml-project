"""
Structured JSON logging setup for all services.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        # Add exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            log_data["exception"] = self.formatException(record.exc_info)
        # Add extra fields if present
        if hasattr(record, "extra_data"):
            log_data["data"] = record.extra_data
        # Add service context
        service = getattr(record, "service", "unknown")
        log_data["service"] = service
        return json.dumps(log_data)


def setup_logging(
    service: str,
    level: str = "INFO",
    force: bool = False,
) -> logging.Logger:
    """Configure structured logging for a service. Returns the root logger."""
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not root_logger.handlers or force:
        # Clear existing handlers
        root_logger.handlers.clear()

        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONFormatter())
        root_logger.addHandler(handler)

    logger = logging.getLogger(service)
    logger.info("Logging initialized for service: %s", service)
    return logger


def get_logger(name: str) -> logging.Logger:
    """Get a logger by name."""
    return logging.getLogger(name)


class LogContext:
    """Context manager that adds extra data to log records."""

    def __init__(self, logger: logging.Logger, **kwargs: Any):
        self.logger = logger
        self.kwargs = kwargs

    def __enter__(self) -> "LogContext":
        # Temporarily add extra to logger
        for key, value in self.kwargs.items():
            setattr(self.logger, key, value)
        return self

    def __exit__(self, *args: Any) -> None:
        for key in self.kwargs:
            if hasattr(self.logger, key):
                delattr(self.logger, key)


def log_with_data(
    logger: logging.Logger,
    level: int,
    message: str,
    **kwargs: Any,
) -> None:
    """Log a message with extra structured data."""
    record = logger.makeRecord(
        logger.name, level, "(unknown)", 0, message, (), None
    )
    record.extra_data = kwargs
    for key, value in kwargs.items():
        setattr(record, key, value)
    logger.handle(record)


__all__ = [
    "setup_logging",
    "get_logger",
    "LogContext",
    "log_with_data",
    "JSONFormatter",
]