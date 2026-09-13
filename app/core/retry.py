"""Bounded exponential backoff for transient external-service failures."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

from app.config import settings

T = TypeVar("T")
logger = logging.getLogger(__name__)


def with_backoff(operation: str, call: Callable[[], T], transient: tuple[type[Exception], ...]) -> T:
    attempts = settings.external_retry_attempts
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except transient:
            if attempt == attempts:
                raise
            delay = settings.external_retry_base_seconds * (2 ** (attempt - 1))
            logger.warning("external_call_retry", extra={
                "operation": operation, "attempt": attempt, "delay_seconds": delay,
            }, exc_info=True)
            time.sleep(delay)
    raise RuntimeError(f"{operation} exhausted retries")
