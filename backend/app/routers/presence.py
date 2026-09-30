from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import SiteVisit
from app.schemas import PresencePing, SitePresenceRead

router = APIRouter(prefix="/presence", tags=["presence"])

# How stale a heartbeat may be before its session stops counting as "online".
# The frontend heartbeats every 30s, so this tolerates one missed beat plus
# latency without flickering the number down and back up.
ONLINE_WINDOW = timedelta(seconds=75)


@router.post("", response_model=SitePresenceRead)
def record_presence(payload: PresencePing, db: Session = Depends(get_db)) -> SitePresenceRead:
    """Upsert this session's heartbeat, then return the two live counters.

    Write and read are one round trip on purpose: the shelf polls this on an
    interval, and a separate GET would double the request count for numbers
    that are always read together.
    """
    # ON CONFLICT rather than SELECT-then-INSERT: two tabs opening at once
    # would otherwise race on the unique constraint and 500 one of them.
    stmt = (
        insert(SiteVisit)
        .values(session_id=payload.session_id)
        .on_conflict_do_update(
            index_elements=[SiteVisit.session_id],
            set_={"last_seen": func.now()},
        )
    )
    db.execute(stmt)
    db.commit()

    total_visits = db.scalar(select(func.count()).select_from(SiteVisit)) or 0
    people_online = (
        db.scalar(
            select(func.count())
            .select_from(SiteVisit)
            .where(SiteVisit.last_seen > func.now() - ONLINE_WINDOW)
        )
        or 0
    )
    return SitePresenceRead(total_visits=total_visits, people_online=people_online)
