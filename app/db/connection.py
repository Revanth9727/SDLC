from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from app.config import settings


def _build_url(raw: str) -> str:
    """Ensure the URL uses the psycopg3 driver understood by SQLAlchemy 2.0."""
    if raw.startswith("postgresql://") and "+psycopg" not in raw:
        return raw.replace("postgresql://", "postgresql+psycopg://", 1)
    return raw


engine = create_engine(_build_url(settings.database_url), pool_pre_ping=True)

SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    class_=Session,
)


def get_session() -> Session:
    """Yield a database session; close it automatically on exit."""
    with SessionLocal() as session:
        yield session
