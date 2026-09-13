"""Access-scoped reads of immutable READY repository snapshots."""
from __future__ import annotations

import uuid
from typing import TypeVar

from sqlalchemy import select

from app.db.connection import SessionLocal
from app.db.models import (
    Repository, RepositoryCodeChunk, RepositoryFile, RepositoryGraphEdge,
    RepositoryReference, RepositorySnapshot,
    RepositorySQLAccess, RepositorySymbol,
)

Fact = TypeVar(
    "Fact", RepositoryFile, RepositoryCodeChunk, RepositoryGraphEdge, RepositorySymbol,
    RepositoryReference, RepositorySQLAccess,
)


def active_snapshot(full_name: str, ref: str, access_scope: str) -> RepositorySnapshot | None:
    """Return only an atomically-published READY snapshot for this exact scope/ref."""
    with SessionLocal() as db:
        row = db.scalar(
            select(RepositorySnapshot)
            .join(Repository, Repository.id == RepositorySnapshot.repo_id)
            .where(
                Repository.full_name == full_name,
                Repository.access_scope == access_scope,
                RepositorySnapshot.ref == ref,
                RepositorySnapshot.index_status == "READY",
                RepositorySnapshot.is_active.is_(True),
            )
        )
        if row:
            db.expunge(row)
        return row


def snapshot_facts(snapshot_id: uuid.UUID, model: type[Fact]) -> list[Fact]:
    """Read facts only when their parent snapshot is READY and active."""
    if model not in {
        RepositoryFile, RepositoryCodeChunk, RepositoryGraphEdge, RepositorySymbol,
        RepositoryReference, RepositorySQLAccess,
    }:
        raise ValueError("unsupported repository fact type")
    with SessionLocal() as db:
        ready = db.scalar(select(RepositorySnapshot.id).where(
            RepositorySnapshot.id == snapshot_id,
            RepositorySnapshot.index_status == "READY",
            RepositorySnapshot.is_active.is_(True),
        ))
        if ready is None:
            return []
        rows = list(db.scalars(select(model).where(model.snapshot_id == snapshot_id)))
        for row in rows:
            db.expunge(row)
        return rows
