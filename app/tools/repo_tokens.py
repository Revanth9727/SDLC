"""Encrypted storage for per-repo GitHub tokens (R-17)."""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.db.connection import SessionLocal
from app.db.models import RepoToken

logger = logging.getLogger(__name__)


class RepoTokenStore:
    """Stores tokens encrypted at rest and only decrypts them in memory."""

    def __init__(self, key: str | None = None) -> None:
        raw_key = key if key is not None else settings.app_encryption_key
        if not raw_key:
            raise ValueError("APP_ENCRYPTION_KEY is required to store private repo tokens")
        self._fernet = Fernet(raw_key.encode("utf-8"))

    def save(self, owner_repo: str, token: str) -> None:
        encrypted = self._fernet.encrypt(token.encode("utf-8")).decode("utf-8")
        with SessionLocal() as db:
            stmt = (
                insert(RepoToken)
                .values(owner_repo=owner_repo, encrypted_token=encrypted)
                .on_conflict_do_update(
                    index_elements=[RepoToken.owner_repo],
                    set_={"encrypted_token": encrypted},
                )
            )
            db.execute(stmt)
            db.commit()
        logger.info("repo_token.save repo=%r token=<masked>", owner_repo)

    def get(self, owner_repo: str) -> str | None:
        with SessionLocal() as db:
            row = db.get(RepoToken, owner_repo)
            if row is None:
                return None
            encrypted = row.encrypted_token
        token = self._fernet.decrypt(encrypted.encode("utf-8")).decode("utf-8")
        logger.info("repo_token.get repo=%r found=%s token=<masked>", owner_repo, bool(token))
        return token

    def has_token(self, owner_repo: str) -> bool:
        with SessionLocal() as db:
            return db.get(RepoToken, owner_repo) is not None
