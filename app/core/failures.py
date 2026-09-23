"""Consistent, specific, credential-safe failure descriptions."""

from __future__ import annotations

import re
import traceback
from pathlib import Path
from typing import Literal

from app.agents.state import FailureContext
from app.config import settings


def describe_failure(operation: str, exc: BaseException) -> str:
    """Return an operation-specific error while redacting known credentials."""
    detail = sanitize_failure_text(str(exc).strip() or repr(exc))
    return f"{operation} failed ({type(exc).__name__}): {detail}"


def sanitize_failure_text(value: str) -> str:
    """Remove configured credentials and common authorization values from diagnostics."""
    detail = value
    for secret in (
        settings.github_token,
        settings.jira_api_token,
        settings.openai_api_key,
        settings.app_encryption_key,
    ):
        if secret:
            detail = detail.replace(secret, "<redacted>")
    detail = re.sub(
        r"(?i)(authorization\s*[:=]\s*)(basic|bearer)?\s*[^\s,;]+",
        r"\1<redacted>",
        detail,
    )
    return detail


def failure_context(
    exc: BaseException,
    *,
    classification: Literal["reasoning", "infrastructure"],
    component: str,
    operation: str,
    reason: str,
    identifiers: dict[str, object] | None = None,
) -> FailureContext:
    """Capture a compact call site without source text, locals, or secret values."""
    frames = traceback.extract_tb(exc.__traceback__)
    frame = frames[-1] if frames else None
    return FailureContext(
        classification=classification,
        component=component,
        operation=operation,
        function=frame.name if frame else operation,
        file=_display_path(frame.filename) if frame else "unknown",
        line=frame.lineno if frame else 0,
        exception_type=type(exc).__name__,
        message=sanitize_failure_text(str(exc).strip() or repr(exc)),
        reason=reason,
        stack=[
            sanitize_failure_text(f"{_display_path(item.filename)}:{item.lineno} in {item.name}")
            for item in frames[-12:]
        ],
        identifiers={
            str(key): sanitize_failure_text(str(value))
            for key, value in (identifiers or {}).items() if value is not None
        },
    )


def failure_summary(context: FailureContext) -> str:
    return (
        f"{context.classification.title()} failure in {context.component}.{context.operation} "
        f"({context.file}:{context.line}, {context.exception_type}): {context.message}"
    )


def _display_path(value: str) -> str:
    path = Path(value)
    parts = path.parts
    for anchor in ("app", "tests"):
        if anchor in parts:
            return "/".join(parts[parts.index(anchor):])
    return path.name
