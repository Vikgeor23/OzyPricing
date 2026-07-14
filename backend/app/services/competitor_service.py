"""CRUD for competitors."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Competitor
from app.schemas.competitor import CompetitorCreate, CompetitorUpdate


def list_competitors(db: Session) -> list[Competitor]:
    stmt = select(Competitor).order_by(Competitor.name)
    return list(db.scalars(stmt).all())


def get_competitor(db: Session, competitor_id: uuid.UUID) -> Competitor | None:
    return db.get(Competitor, competitor_id)


def create_competitor(db: Session, data: CompetitorCreate) -> Competitor:
    competitor = Competitor(**data.model_dump())
    db.add(competitor)
    db.commit()
    db.refresh(competitor)
    return competitor


def update_competitor(db: Session, competitor: Competitor, data: CompetitorUpdate) -> Competitor:
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(competitor, key, value)
    db.commit()
    db.refresh(competitor)
    return competitor


def delete_competitor(db: Session, competitor: Competitor) -> None:
    db.delete(competitor)
    db.commit()
