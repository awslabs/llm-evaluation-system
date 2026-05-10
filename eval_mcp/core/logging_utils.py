"""Shared logging utilities for structured JSON logging.

All logs go to stdout in JSON format for CloudWatch Logs Insights compatibility.
Kubernetes + Fluent Bit ships these to CloudWatch automatically.

Usage:
    from eval_mcp.core.logging_utils import get_logger, log_event

    logger = get_logger(__name__)

    # Simple event logging
    log_event(logger, "info", "qa_generation_started", user_id="abc", document="doc.pdf")

    # Log with large data (auto-truncated)
    log_event(logger, "error", "qa_generation_failed",
              user_id="abc",
              bedrock_response=large_response,  # Will be truncated to 2KB
              error="No QA pairs generated")
"""

import json
import logging
from typing import Any, Optional


# Maximum size for logged values (2KB) to avoid bloating logs
MAX_VALUE_SIZE = 2048


def truncate_value(value: Any, max_size: int = MAX_VALUE_SIZE) -> Any:
    """Truncate large values to avoid log bloat.

    Args:
        value: Any value to potentially truncate
        max_size: Maximum string length

    Returns:
        Original value if small enough, truncated string otherwise
    """
    if value is None:
        return None

    # Convert to string for size check
    if isinstance(value, (dict, list)):
        str_value = json.dumps(value, default=str)
    else:
        str_value = str(value)

    if len(str_value) <= max_size:
        return value

    # Truncate and indicate it was truncated
    truncated = str_value[:max_size]
    return f"{truncated}... [TRUNCATED, original size: {len(str_value)}]"


class JsonFormatter(logging.Formatter):
    """JSON formatter for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add extra fields from record
        if hasattr(record, "extra_fields"):
            log_data.update(record.extra_fields)

        return json.dumps(log_data, default=str)


def get_logger(name: str) -> logging.Logger:
    """Get a logger configured for JSON output.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)

    # Avoid duplicate handlers
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # Console handler (stdout) - Kubernetes captures this
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(JsonFormatter(datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(console_handler)

    # Prevent propagation to root logger (avoid duplicate logs)
    logger.propagate = False

    return logger


def log_event(
    logger: logging.Logger,
    level: str,
    event: str,
    user_id: Optional[str] = None,
    **kwargs: Any
) -> None:
    """Log a structured event with consistent format.

    Args:
        logger: Logger instance
        level: Log level (debug, info, warning, error)
        event: Event name (e.g., "qa_generation_failed")
        user_id: User ID for correlation
        **kwargs: Additional fields to log (large values auto-truncated)

    Example:
        log_event(logger, "error", "qa_generation_failed",
                  user_id="abc123",
                  document="manual.pdf",
                  bedrock_response=response,
                  error="No QA pairs generated")
    """
    # Build extra fields
    extra_fields = {"event": event}

    if user_id:
        extra_fields["user_id"] = user_id

    # Add kwargs, truncating large values
    for key, value in kwargs.items():
        extra_fields[key] = truncate_value(value)

    # Create log record with extra fields
    log_func = getattr(logger, level.lower(), logger.info)

    # Use LogRecord extra to pass structured data
    old_factory = logging.getLogRecordFactory()

    def record_factory(*args, **record_kwargs):
        record = old_factory(*args, **record_kwargs)
        record.extra_fields = extra_fields
        return record

    logging.setLogRecordFactory(record_factory)
    log_func(event)
    logging.setLogRecordFactory(old_factory)


# Pre-configured logger for MCP tools
mcp_logger = get_logger("mcp_tools.synthetic")
