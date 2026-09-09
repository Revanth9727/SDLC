import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Enum as SAEnum
from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, Text, text
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

    __table_args__ = (
        Index(
            "uq_subtasks_one_active_per_ticket",
            "ticket_id",
            unique=True,
            postgresql_where=text("status IN ('running', 'waiting', 'pending')"),
        ),
    )


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
    __table_args__ = (
        Index('ix_pending_approvals_issue', 'jira_issue_key', postgresql_where=text("status = 'PENDING'")),
    )
