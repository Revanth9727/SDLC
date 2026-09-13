"""Durable identity markers for Jira comments posted by this application."""
from sqlalchemy.dialects.postgresql import insert

from app.db.connection import SessionLocal
from app.db.models import JiraPostedComment


def record_app_comment(comment_id: str, issue_key: str) -> None:
    with SessionLocal() as db:
        db.execute(
            insert(JiraPostedComment)
            .values(comment_id=str(comment_id), issue_key=issue_key)
            .on_conflict_do_nothing()
        )
        db.commit()


def is_app_comment(comment_id: str | None) -> bool:
    if not comment_id:
        return False
    with SessionLocal() as db:
        return db.get(JiraPostedComment, str(comment_id)) is not None
