"""Create all database tables declared in models.py.

Run directly to initialise (or re-run safely — CREATE TABLE IF NOT EXISTS):
    python -m app.db.init_db
"""

import logging

from app.db.connection import engine
from app.db.models import Base

logger = logging.getLogger(__name__)


def create_all() -> None:
    """Create every table in Base.metadata if it does not already exist."""
    logger.info("Running create_all() against %s", engine.url)
    Base.metadata.create_all(bind=engine)
    logger.info("All tables created (or already existed).")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    create_all()
    print("\nDone. Tables are ready in Postgres.")
