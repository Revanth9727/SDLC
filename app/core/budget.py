"""Atomic per-ticket call/token/cost/time accounting."""
from datetime import datetime, timezone
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from app.config import settings
from app.db.connection import SessionLocal
from app.db.models import Ticket, TicketBudget


class BudgetExceeded(ValueError):
    pass


def budget_reason(usage, limits=None):
    limits = limits or {}
    if any(usage.get(field, 0) >= limits.get(field, default) for field, default in (
        ('calls', settings.ticket_call_budget), ('tokens', settings.ticket_token_budget),
        ('est_cost_usd', settings.ticket_cost_budget_usd),
        ('elapsed_seconds', settings.ticket_time_budget_seconds))):
        return (f"Budget reached: used {usage['calls']} calls / ${usage['est_cost_usd']:.4f} / "
                f"{usage['tokens']} tokens / {usage.get('elapsed_seconds', 0):.0f}s — continue?")
    return None


def _id(ticket_id):
    try:
        return UUID(ticket_id)
    except (ValueError, TypeError):
        return None


def usage(ticket_id):
    ident = _id(ticket_id)
    if not ident:
        return None
    with SessionLocal() as db:
        row = db.get(TicketBudget, ident)
        return _payload(row) if row else None


def _payload(row):
    started = row.started_at
    if started and started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    elapsed = max(0.0, (datetime.now(timezone.utc) - started).total_seconds()) if started else 0.0
    return {'calls': row.calls, 'tokens': row.tokens, 'est_cost_usd': row.est_cost_usd,
            'elapsed_seconds': elapsed, 'limits': row.limits or {}}


def reserve(ticket_id):
    ident = _id(ticket_id)
    if not ident:
        return False
    with SessionLocal() as db:
        if db.get(Ticket, ident) is None:
            return False
        db.execute(insert(TicketBudget).values(ticket_id=ident, calls=0, tokens=0, est_cost_usd=0, limits={})
                   .on_conflict_do_nothing())
        row = db.scalar(select(TicketBudget).where(TicketBudget.ticket_id == ident).with_for_update())
        reason = budget_reason(_payload(row), row.limits)
        if reason:
            raise BudgetExceeded(reason)
        row.calls += 1
        db.commit()
    return True


def finish(ticket_id, tokens, cost):
    with SessionLocal() as db:
        row = db.scalar(select(TicketBudget).where(TicketBudget.ticket_id == UUID(ticket_id)).with_for_update())
        row.tokens += tokens
        row.est_cost_usd += cost
        db.commit()
        payload = _payload(row)
    from app.core.status_events import emit_ticket_event
    emit_ticket_event(ticket_id, 'llm', 'budget', payload)


def extend(ticket_id):
    """Explicit human permission grants one additional configured allowance."""
    with SessionLocal() as db:
        row = db.scalar(select(TicketBudget).where(TicketBudget.ticket_id == UUID(ticket_id)).with_for_update())
        if row:
            current = _payload(row)
            row.limits = {field: current[field] + default for field, default in (
                ('calls', settings.ticket_call_budget), ('tokens', settings.ticket_token_budget),
                ('est_cost_usd', settings.ticket_cost_budget_usd),
                ('elapsed_seconds', settings.ticket_time_budget_seconds))}
            db.commit()
