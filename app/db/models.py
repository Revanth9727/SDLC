import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import Enum as SAEnum
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Ticket(Base):
    """One incoming work request (from Jira or entered manually)."""

    __tablename__ = "tickets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # "manual" | "jira"
    external_key: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )  # e.g. "SANDBOX-42"; null for manual tickets
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="new"
    )  # "new" | "processing" | "done" | "needs_human"
    repos: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(Text), nullable=True, default=None
    )  # confirmed GitHub repos e.g. ["owner/repo"] — set by the confirm-repos gate
    claimed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )  # set atomically by the poller when it claims this ticket; None = unclaimed
    last_stuck_comment_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )  # when the last stuck-episode comment was posted; None = not yet commented this episode
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    subtasks: Mapped[list["Subtask"]] = relationship(
        "Subtask", back_populates="ticket", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index(
            "ix_tickets_external_key_unique",
            "external_key",
            unique=True,
            postgresql_where=text("external_key IS NOT NULL"),
        ),
    )


class Subtask(Base):
    """One isolated unit of work extracted from a Ticket by the Planner.

    The ``state`` column stores the full SubTaskState object as JSONB so that
    LangGraph checkpointing can read and restore it without a separate table.
    ``depends_on`` is a JSON list of subtask UUID strings (set by the Planner's
    dependency graph). Isolation is enforced by always keying reads on ``id``
    (R-4).
    """

    __tablename__ = "subtasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # "bug" | "feature" | "ci" | "design"
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="new"
    )  # mirrors SubTaskState.status
    depends_on: Mapped[Optional[list[Any]]] = mapped_column(
        JSON, nullable=True, default=list
    )  # list of subtask UUID strings
    state: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )  # full SubTaskState serialised to JSON
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    ticket: Mapped["Ticket"] = relationship("Ticket", back_populates="subtasks")



class TicketEvent(Base):
    """One structured event emitted during a ticket's lifecycle (R-7, R-23).

    Every agent activation, tool call, guard decision, and stage transition is
    persisted here so the UI can replay history after a reload and the audit
    trail is never lost when subscribers disconnect.
    """

    __tablename__ = "ticket_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subtask_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subtasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    agent: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class RepoToken(Base):
    """Encrypted per-repo GitHub token for private repo access (R-17)."""

    __tablename__ = "repo_tokens"

    owner_repo: Mapped[str] = mapped_column(String(256), primary_key=True)
    encrypted_token: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class JiraPostedComment(Base):
    """A Jira comment posted by this app, used to prevent webhook feedback loops."""

    __tablename__ = "jira_posted_comments"

    comment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class WebhookDelivery(Base):
    __tablename__ = 'webhook_deliveries'
    id: Mapped[str] = mapped_column(String(256), primary_key=True)
    source: Mapped[str] = mapped_column(String(16))
    event: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default='pending')
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PRLink(Base):
    __tablename__ = 'pr_links'
    github_pr_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    jira_issue_key: Mapped[str] = mapped_column(String(64), index=True)
    repo: Mapped[str] = mapped_column(String(256))
    number: Mapped[int] = mapped_column()
    branch: Mapped[str] = mapped_column(String(256))
    url: Mapped[str] = mapped_column(Text)
    pr_state: Mapped[str] = mapped_column(String(16))
    notified_state: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    jira_category: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PendingApproval(Base):
    __tablename__ = 'pending_approvals'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('tickets.id', ondelete='CASCADE'))
    subtask_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey('subtasks.id', ondelete='CASCADE'), nullable=True, unique=True)
    jira_issue_key: Mapped[str] = mapped_column(String(64))
    proposed_action: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(SAEnum('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', name='approval_status'), default='PENDING')
    jira_comment_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_comment_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True)
    decision_note: Mapped[str] = mapped_column(Text, default='')
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reminded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        Index('ix_pending_approvals_issue', 'jira_issue_key', postgresql_where=text("status = 'PENDING'")),
    )


class ProjectStatusMap(Base):
    """One confirmed workflow map per configured Jira project (R-49)."""
    __tablename__ = 'project_status_maps'
    project_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    rows: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)


class TicketBudget(Base):
    __tablename__ = 'ticket_budgets'
    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('tickets.id', ondelete='CASCADE'), primary_key=True)
    calls: Mapped[int] = mapped_column(default=0)
    tokens: Mapped[int] = mapped_column(default=0)
    est_cost_usd: Mapped[float] = mapped_column(default=0.0)
    limits: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class SubtaskMemory(Base):
    """One resolved sub-task's compact record, for solution reuse (memory.md
    §4, ai_rules.md R-29). Only ever written AFTER a sub-task's PR opens, so
    the table is inherently "resolved tickets only" — reading it never crosses
    into another sub-task's LIVE state (isolation, R-4/memory.md §6). Stores a
    prose summary and file paths only — never code bodies or secrets (M-3)."""
    __tablename__ = 'subtask_memory'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subtask_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('subtasks.id', ondelete='CASCADE'), unique=True)
    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('tickets.id', ondelete='CASCADE'))
    subtask_type: Mapped[str] = mapped_column(String(32))
    problem_summary: Mapped[str] = mapped_column(Text)
    resolution_summary: Mapped[str] = mapped_column(Text)
    files_touched: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default='success', server_default='success')
    # Dimension pinned to text-embedding-3-small (memory.md §4, M-5) — a model
    # swap is a deliberate migration, never an accidental mismatch.
    embedding: Mapped[list[float]] = mapped_column(Vector(1536))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index('ix_subtask_memory_type', 'subtask_type'),
        Index(
            'ix_subtask_memory_embedding_hnsw', 'embedding',
            postgresql_using='hnsw',
            postgresql_with={'m': 16, 'ef_construction': 64},
            postgresql_ops={'embedding': 'vector_cosine_ops'},
        ),
    )


class ExactCache(Base):
    """Fast process-independent cache for byte-identical LLM prompts (R-36)."""
    __tablename__ = 'exact_cache'
    prompt_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    response: Mapped[str] = mapped_column(Text, nullable=False)
    model_used: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    __table_args__ = {"prefixes": ["UNLOGGED"]}


class SemanticCache(Base):
    """Near-duplicate prompt cache using pgvector cosine similarity (R-36)."""
    __tablename__ = 'semantic_cache'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False)
    model_used: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (
        Index(
            'ix_semantic_cache_embedding_hnsw', 'embedding',
            postgresql_using='hnsw',
            postgresql_with={'m': 16, 'ef_construction': 64},
            postgresql_ops={'embedding': 'vector_cosine_ops'},
        ),
    )


class Repository(Base):
    """Access-scoped identity for persistent repository intelligence (R-50)."""
    __tablename__ = "repositories"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(String(256), nullable=False)
    access_scope: Mapped[str] = mapped_column(String(128), nullable=False, default="public")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint("full_name", "access_scope", name="uq_repository_scope"),)


class RepositorySnapshot(Base):
    __tablename__ = "repository_snapshots"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    ref: Mapped[str] = mapped_column(String(512), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    index_status: Mapped[str] = mapped_column(String(16), nullable=False, default="NEW")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    base_snapshot_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("repository_snapshots.id", ondelete="SET NULL"), nullable=True)
    indexed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    __table_args__ = (
        UniqueConstraint("repo_id", "ref", "commit_sha", name="uq_repository_snapshot_commit"),
        Index("uq_repository_snapshot_active", "repo_id", "ref", unique=True,
              postgresql_where=text("is_active")),
    )


class ProvenanceMixin:
    source_file: Mapped[str] = mapped_column(Text, nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    extractor: Mapped[str] = mapped_column(String(64), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    evidence_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="PROVEN")


class RepositoryFile(ProvenanceMixin, Base):
    __tablename__ = "repository_files"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("repository_snapshots.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False, default="source")
    artifact_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    __table_args__ = (UniqueConstraint("snapshot_id", "path", name="uq_repository_file_path"),)


class RepositoryCodeChunk(ProvenanceMixin, Base):
    """Searchable structural code span; source text remains in the checkout."""
    __tablename__ = "repository_code_chunks"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repository_snapshots.id", ondelete="CASCADE"), index=True
    )
    chunk_key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False, default="")
    language: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    __table_args__ = (
        UniqueConstraint("snapshot_id", "chunk_key", name="uq_repository_code_chunk_key"),
        Index(
            "ix_repository_code_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class RepositorySymbol(ProvenanceMixin, Base):
    __tablename__ = "repository_symbols"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("repository_snapshots.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    qualified_name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    signature: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class RepositoryReference(ProvenanceMixin, Base):
    __tablename__ = "repository_references"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("repository_snapshots.id", ondelete="CASCADE"), index=True)
    source_symbol: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    target_symbol: Mapped[str] = mapped_column(Text, nullable=False)
    relation_kind: Mapped[str] = mapped_column(String(32), nullable=False)


class RepositoryGraphEdge(ProvenanceMixin, Base):
    """A parser-proven relationship used only for repository navigation."""
    __tablename__ = "repository_graph_edges"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repository_snapshots.id", ondelete="CASCADE"), index=True
    )
    source_node: Mapped[str] = mapped_column(Text, nullable=False)
    target_node: Mapped[str] = mapped_column(Text, nullable=False)
    relation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    __table_args__ = (
        Index("ix_repository_graph_source", "snapshot_id", "relation_kind", "source_node"),
        Index("ix_repository_graph_target", "snapshot_id", "relation_kind", "target_node"),
    )


class RepositorySQLAccess(ProvenanceMixin, Base):
    __tablename__ = "repository_sql_access"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("repository_snapshots.id", ondelete="CASCADE"), index=True)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    relation_name: Mapped[str] = mapped_column(Text, nullable=False)
    column_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enclosing_symbol: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class RepositoryIndexPolicy(Base):
    __tablename__ = "repository_index_policies"
    repo_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True)
    overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class RepositoryIndexJob(Base):
    __tablename__ = "repository_index_jobs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    ref: Mapped[str] = mapped_column(String(512), nullable=False)
    target_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("repository_snapshots.id", ondelete="SET NULL"), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
