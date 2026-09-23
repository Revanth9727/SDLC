"""Create all database tables declared in models.py.

Run directly to initialise (or re-run safely — CREATE TABLE IF NOT EXISTS):
    python -m app.db.init_db
"""

import logging

from app.db.connection import engine
from app.db.models import Base
from app.core.subtasks import cleanup_active_subtask_pile
from app.core.approvals import cleanup_pending_gate_pile

logger = logging.getLogger(__name__)


def create_all() -> None:
    """Create every table in Base.metadata if it does not already exist."""
    logger.info("Running create_all() against %s", engine.url)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.create_all(bind=engine)
    ensure_memory_schema()
    ensure_llm_cache_schema()
    ensure_guard_schema()
    ensure_repository_intelligence_schema()
    cleaned = cleanup_active_subtask_pile()
    if cleaned:
        logger.info("Superseded %d duplicate active subtask(s).", cleaned)
    cleaned_gates = cleanup_pending_gate_pile()
    if cleaned_gates:
        logger.info("Expired %d duplicate pending approval gate(s).", cleaned_gates)
    logger.info("All tables created (or already existed).")


def ensure_guard_schema() -> None:
    """Upgrade concurrency, gate-timeout, and wall-time budget storage."""
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX IF EXISTS uq_pending_approvals_one_per_ticket")
        connection.exec_driver_sql("DROP INDEX IF EXISTS uq_subtasks_one_active_per_ticket")
        connection.exec_driver_sql(
            "ALTER TABLE ticket_budgets ADD COLUMN IF NOT EXISTS started_at "
            "TIMESTAMPTZ NOT NULL DEFAULT now()"
        )
        connection.exec_driver_sql(
            "ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS reminded_at TIMESTAMPTZ"
        )


def ensure_memory_schema() -> None:
    """Apply the idempotent memory migration to databases created pre-outcomes."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE subtask_memory ADD COLUMN IF NOT EXISTS outcome "
            "VARCHAR(32) NOT NULL DEFAULT 'success'"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_subtask_memory_embedding_hnsw "
            "ON subtask_memory USING hnsw (embedding vector_cosine_ops)"
        )


def ensure_llm_cache_schema() -> None:
    """Create/upgrade the in-Postgres LLM caches without a separate service."""
    from app.db.models import ExactCache, SemanticCache

    ExactCache.__table__.create(engine, checkfirst=True)
    SemanticCache.__table__.create(engine, checkfirst=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE exact_cache SET UNLOGGED")
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_semantic_cache_embedding_hnsw "
            "ON semantic_cache USING hnsw (embedding vector_cosine_ops)"
        )


def ensure_repository_intelligence_schema() -> None:
    """Create the deterministic repository-intelligence storage layer (R-50)."""
    from app.db.models import (
        Repository, RepositoryCodeChunk, RepositoryFile, RepositoryGraphEdge,
        RepositoryIndexJob, RepositoryIndexPolicy,
        RepositoryReference, RepositorySnapshot, RepositorySQLAccess, RepositorySymbol,
    )
    for model in (Repository, RepositorySnapshot, RepositoryFile, RepositoryCodeChunk, RepositorySymbol,
                  RepositoryReference, RepositoryGraphEdge, RepositorySQLAccess, RepositoryIndexPolicy,
                  RepositoryIndexJob):
        model.__table__.create(engine, checkfirst=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE repository_sql_access ADD COLUMN IF NOT EXISTS column_name TEXT"
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    create_all()
    print("\nDone. Tables are ready in Postgres.")
