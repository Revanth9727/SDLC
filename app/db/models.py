import uuid
from datetime import datetime, timezone
from typing import Any, Optional

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
