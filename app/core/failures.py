"""Consistent, specific, credential-safe failure descriptions."""

from __future__ import annotations

import re

from app.config import settings


def describe_failure(operation: str, exc: BaseException) -> str:
    """Return an operation-specific error while redacting known credentials."""
    detail = str(exc).strip() or repr(exc)
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
    return f"{operation} failed ({type(exc).__name__}): {detail}"
