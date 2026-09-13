"""Two-tier Postgres/pgvector response cache used only by LLMClient."""
from __future__ import annotations

import hashlib
import logging
from collections import Counter
from threading import Lock

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.db.connection import SessionLocal
from app.db.models import ExactCache, SemanticCache

logger = logging.getLogger(__name__)
_counts: Counter[str] = Counter()
_counts_lock = Lock()


def prompt_material(system: str, user: str, tier: str) -> str:
    return f"tier:{tier}\nsystem:\n{system}\nuser:\n{user}"


def prompt_hash(system: str, user: str, tier: str) -> str:
    return hashlib.sha256(prompt_material(system, user, tier).encode("utf-8")).hexdigest()


def enabled_for(ticket_id: str | None) -> bool:
    if not getattr(settings, "llm_cache_enabled", True):
        return False
    return ticket_id is None or getattr(settings, "llm_cache_ticket_prompts", False)


def note(kind: str) -> None:
    with _counts_lock:
        _counts[kind] += 1


def cache_stats() -> dict[str, int | float]:
    with _counts_lock:
        exact = _counts["exact_hits"]
        semantic = _counts["semantic_hits"]
        misses = _counts["misses"]
        bypassed = _counts["bypassed"]
    eligible = exact + semantic + misses
    return {
        "exact_hits": exact,
        "semantic_hits": semantic,
        "misses": misses,
        "bypassed": bypassed,
        "requests": eligible + bypassed,
        "hit_rate": round((exact + semantic) / eligible, 4) if eligible else 0.0,
    }


def reset_stats() -> None:
    with _counts_lock:
        _counts.clear()


def get_exact(key: str, model: str) -> str | None:
    with SessionLocal() as db:
        return db.scalar(select(ExactCache.response).where(
            ExactCache.prompt_hash == key, ExactCache.model_used == model))


def get_semantic(vector: list[float], model: str, threshold: float) -> str | None:
    distance = SemanticCache.embedding.cosine_distance(vector)
    with SessionLocal() as db:
        row = db.execute(
            select(SemanticCache.response, (1 - distance).label("similarity"))
            .where(SemanticCache.model_used == model)
            .order_by(distance)
            .limit(1)
        ).first()
    return row.response if row and float(row.similarity) >= threshold else None


def put(key: str, prompt: str, vector: list[float] | None, response: str, model: str) -> None:
    with SessionLocal() as db:
        db.execute(
            insert(ExactCache).values(
                prompt_hash=key, response=response, model_used=model,
            ).on_conflict_do_update(
                index_elements=[ExactCache.prompt_hash],
                set_={"response": response, "model_used": model},
            )
        )
        if vector is not None:
            db.add(SemanticCache(prompt=prompt, embedding=vector, response=response, model_used=model))
        db.commit()


def invalidate(system: str, user: str, tier: str, response: str) -> None:
    """Remove a response proven invalid by complete_json schema validation."""
    key = prompt_hash(system, user, tier)
    prompt = prompt_material(system, user, tier)
    with SessionLocal() as db:
        db.execute(delete(ExactCache).where(ExactCache.prompt_hash == key))
        db.execute(delete(SemanticCache).where(
            SemanticCache.prompt == prompt, SemanticCache.response == response))
        db.commit()


def warning(operation: str, exc: Exception) -> None:
    logger.warning("LLM cache %s failed; continuing without cache: %s", operation, exc)
