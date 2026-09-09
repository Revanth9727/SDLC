"""Single OpenAI access layer for all agents (R-11, R-33, R-34)."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Literal, TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from app.config import settings

ModelTier = Literal["strong", "cheap"]
SchemaT = TypeVar("SchemaT", bound=BaseModel)

_current_ticket_id: ContextVar[str | None] = ContextVar(
    "llm_current_ticket_id",
    default=None,
)


@dataclass
class _Usage:
    calls: int = 0
    tokens: int = 0
    est_cost_usd: float = 0.0


class LLMClient:
    """OpenAI wrapper used by every agent.

    Agents pass a tier (normally from ``app.agents.router``); this class is the
    only place that resolves tiers to concrete model names and records per-ticket
    usage.
    """

    _usage: dict[str, _Usage] = defaultdict(_Usage)

    # Rough defaults for budget accounting only. These estimates are deliberately
    # centralized so later pricing/config changes do not touch agent code.
    _EST_COST_PER_1K_TOKENS: dict[str, float] = {
        "gpt-4o": 0.0075,
        "gpt-4o-mini": 0.0003,
    }

    def __init__(self, ticket_id: str | None = None, client: OpenAI | None = None) -> None:
        self.ticket_id = ticket_id
        self._client = client or OpenAI(api_key=settings.openai_api_key)

    @contextmanager
    def use_ticket(self, ticket_id: str) -> Iterator[None]:
        """Set the current ticket id for calls made inside the context."""
        token = _current_ticket_id.set(ticket_id)
        try:
            yield
        finally:
            _current_ticket_id.reset(token)

    def complete(
        self,
        system: str,
        user: str,
        tier: ModelTier = "strong",
        *,
        ticket_id: str | None = None,
        json_mode: bool = False,
    ) -> str:
        """Return plain text from OpenAI and record usage for the current ticket."""
        model = self._model_for_tier(tier)
        kwargs: dict[str, Any] = {"response_format": {"type": "json_object"}} if json_mode else {}
        response = self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **kwargs,
        )
        self._record_usage(self._resolve_ticket_id(ticket_id), model, response)
        return response.choices[0].message.content or ""

    def complete_json(
        self,
        system: str,
        user: str,
        schema: type[SchemaT],
        tier: ModelTier = "strong",
        *,
        ticket_id: str | None = None,
    ) -> SchemaT:
        """Return a response validated against ``schema``; retry once on failure."""
        json_system = (
            f"{system}\n\nReturn only valid JSON matching this JSON Schema:\n"
            f"{json.dumps(schema.model_json_schema(), separators=(',', ':'))}"
        )
        last_error: ValidationError | ValueError | None = None
        prompt = user
        for attempt in range(2):
            # json_mode asks the API to guarantee valid JSON (no markdown fences
            # or prose); _strip_code_fence is a defensive second layer in case a
            # model/provider ignores that or doesn't support it.
            text = self.complete(json_system, prompt, tier=tier, ticket_id=ticket_id, json_mode=True)
            try:
                return schema.model_validate_json(self._strip_code_fence(text))
            except (ValidationError, ValueError) as exc:
                last_error = exc
                prompt = (
                    f"{user}\n\nYour previous response did not validate as JSON for "
                    f"{schema.__name__}: {exc}. Return corrected JSON only, no markdown "
                    "code fences or commentary."
                )
        raise last_error  # type: ignore[misc]

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        """Unwrap a ```json ... ``` (or bare ``` ... ```) fence some models add
        despite instructions not to. A no-op on already-bare JSON."""
        stripped = text.strip()
        match = re.match(r"^```[a-zA-Z0-9]*\s*\n(.*)\n```\s*$", stripped, re.DOTALL)
        return match.group(1).strip() if match else stripped

    @classmethod
    def get_usage(cls, ticket_id: str) -> dict[str, float | int]:
        usage = cls._usage.get(ticket_id, _Usage())
        return {
            "calls": usage.calls,
            "tokens": usage.tokens,
            "est_cost_usd": round(usage.est_cost_usd, 6),
        }

    @classmethod
    def reset_usage(cls, ticket_id: str | None = None) -> None:
        if ticket_id is None:
            cls._usage.clear()
        else:
            cls._usage.pop(ticket_id, None)

    def _model_for_tier(self, tier: ModelTier) -> str:
        if tier == "strong":
            return settings.model_strong
        if tier == "cheap":
            return settings.model_cheap
        raise ValueError("tier must be 'strong' or 'cheap'")

    def _resolve_ticket_id(self, ticket_id: str | None) -> str | None:
        return ticket_id or self.ticket_id or _current_ticket_id.get()

    @classmethod
    def _record_usage(cls, ticket_id: str | None, model: str, response: Any) -> None:
        if not ticket_id:
            return
        usage_obj = getattr(response, "usage", None)
        total_tokens = int(getattr(usage_obj, "total_tokens", 0) or 0)
        rate = cls._EST_COST_PER_1K_TOKENS.get(model, 0.0)
        usage = cls._usage[ticket_id]
        usage.calls += 1
        usage.tokens += total_tokens
        usage.est_cost_usd += (total_tokens / 1000.0) * rate
