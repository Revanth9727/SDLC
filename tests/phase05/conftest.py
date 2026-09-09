import uuid
import pytest
from app.db.connection import engine, SessionLocal
from app.db.models import Ticket, PendingApproval, PRLink, WebhookDelivery


@pytest.fixture
def db_ticket():
    for table in (PendingApproval.__table__, PRLink.__table__, WebhookDelivery.__table__):
        table.create(engine, checkfirst=True)
    with SessionLocal() as db:
        ticket = Ticket(source='jira', external_key=f'TEST-{uuid.uuid4().int % 100000000}',
                        title='Divide by zero', description='Guard zero denominator', repos=['owner/repo'])
        db.add(ticket)
        db.commit()
        db.refresh(ticket)
        ticket_id, key = str(ticket.id), ticket.external_key
    yield ticket_id, key
    with SessionLocal() as db:
        for row in db.query(PRLink).filter(PRLink.jira_issue_key == key):
            db.delete(row)
        db.delete(db.get(Ticket, uuid.UUID(ticket_id)))
        db.commit()
