from datetime import datetime

from app.events import make_event


def test_make_event_has_standard_shape() -> None:
    event = make_event(
        "diagnosis",
        "started",
        "reading repository files",
        ticket_id="ticket-123",
        extra_field="kept",
    )

    assert event["agent"] == "diagnosis"
    assert event["stage"] == "started"
    assert event["message"] == "reading repository files"
    assert event["ticket_id"] == "ticket-123"
    assert event["extra_field"] == "kept"
    assert datetime.fromisoformat(event["ts"])
