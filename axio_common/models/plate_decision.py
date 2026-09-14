"""Plate decisions — the calls a human makes on a plate that is waiting on one.

A plate lands in the lifecycle stage ``awaiting_decision`` when its latest
live test failed or was inconclusive, when it came back from a customer, or
when someone flagged it by hand. This table records what was decided, by
whom, and why. It is append-only: a plate that gets recalibrated, retested
and decided again has every call on record, in order.

A decision is an EVENT in the plate lifecycle (axio-server
``app/services/plate_stage.py``): the newest thing that happened to a plate
names its stage, and a decision newer than the failed test / return that put
the plate on the board moves it on immediately —

    flag          -> awaiting_decision (reason: flagged)
    recalibrate   -> calibrating   (until the next calibration day takes over)
    rework        -> assembled     (until the load-cell swap / calibration takes over)
    ready_to_ship -> ready_to_ship
    hold          -> on_hold       (parked for internal use; anything newer moves it)
    scrap         -> retired       (terminal)

``device_axf_id`` is deliberately NOT a ForeignKey to ``devices.axf_id`` —
the same reasoning as ``ForcePlate.device_axf_id``: a plate can exist (be
built, be flagged) before any calibration has created its ``devices`` row.

``decision`` is a plain String validated against ``PLATE_DECISIONS`` at the
API layer, matching every other vocabulary in this package (no DB enums).
``decided_by`` is a typed name, like ``LiveTestSession.tester_name`` — the
dashboard has shared-password roles, not individual logins.
"""
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import (
    Column, DateTime, ForeignKey, Index, Integer, String, Text,
)
from sqlalchemy.dialects.postgresql import UUID

from axio_common.database import Base
from axio_common.utils.model_utils import current_time


DECISION_FLAG = "flag"
DECISION_RECALIBRATE = "recalibrate"
DECISION_REWORK = "rework"
DECISION_READY_TO_SHIP = "ready_to_ship"
DECISION_HOLD = "hold"
DECISION_SCRAP = "scrap"

PLATE_DECISIONS = (
    DECISION_FLAG,
    DECISION_RECALIBRATE,
    DECISION_REWORK,
    DECISION_READY_TO_SHIP,
    DECISION_HOLD,
    DECISION_SCRAP,
)


class PlateDecision(Base):
    """One call made on one plate. Append-only."""
    __tablename__ = "plate_decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_axf_id = Column(String, nullable=False, index=True)   # canonical dotted, no FK
    decision = Column(String, nullable=False, index=True)        # one of PLATE_DECISIONS
    reason = Column(Text, nullable=True)                         # free text
    decided_by = Column(String, nullable=False)                  # typed name
    decided_at = Column(
        DateTime(timezone=True), nullable=False, default=current_time, index=True,
    )

    # What the decider was looking at. Auto-filled by the API from the plate's
    # latest live test / latest returned delivery; both nullable because a
    # flagged plate may have neither.
    live_test_session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("live_test_sessions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    delivery_id = Column(
        Integer,
        ForeignKey("deliveries.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)

    __table_args__ = (
        # "What was decided about this plate, in order" — the history query.
        Index("ix_plate_decisions_device_time", "device_axf_id", "decided_at"),
    )


class PlateDecisionResponse(BaseModel):
    id: int
    device_axf_id: str
    decision: str
    reason: Optional[str] = None
    decided_by: str
    decided_at: datetime
    live_test_session_id: Optional[uuid.UUID] = None
    delivery_id: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True
