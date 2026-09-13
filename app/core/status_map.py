"""Confirmed project workflow meanings; category classification stays in JiraTool."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.dialects.postgresql import insert

from app.db.connection import SessionLocal
from app.db.models import ProjectStatusMap

Meaning = Literal['ready-to-pick-up', 'work-started', 'awaiting-approval', 'in-review', 'blocked/needs-human', 'done']
MEANINGS = list(Meaning.__args__)
STAGE_MEANING = dict(zip(
    ['ready', 'in_progress', 'awaiting_approval', 'in_review', 'blocked', 'done'], MEANINGS
))


class StatusRow(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    meaning: Meaning


class StatusMap(BaseModel):
    rows: list[StatusRow] = Field(max_length=500)

    @model_validator(mode='after')
    def unique_statuses(self):
        if len({row.id for row in self.rows}) != len(self.rows):
            raise ValueError('Each Jira status can appear only once')
        return self


def load_map(project: str) -> StatusMap | None:
    with SessionLocal() as db:
        row = db.get(ProjectStatusMap, project)
        return StatusMap.model_validate({'rows': row.rows}) if row else None


def save_map(project: str, mapping: StatusMap) -> None:
    rows = mapping.model_dump()['rows']
    with SessionLocal() as db:
        db.execute(insert(ProjectStatusMap).values(project_key=project, rows=rows)
                   .on_conflict_do_update(index_elements=['project_key'], set_={'rows': rows}))
        db.commit()


def suggest(name: str, category: str | None) -> tuple[str, bool]:
    """Names are hints only, never transition destinations. Ambiguity is visible."""
    name = name.casefold()
    for words, meaning in [
        (('approval', 'approve'), 'awaiting-approval'),
        (('blocked', 'hold', 'impediment'), 'blocked/needs-human'),
        (('review', 'qa', 'verification'), 'in-review'),
    ]:
        if category == 'indeterminate' and any(word in name for word in words):
            return meaning, False
    return {'new': 'ready-to-pick-up', 'done': 'done'}.get(category, 'work-started'), category not in {'new', 'done'}
