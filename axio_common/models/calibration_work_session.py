"""Calibration work sessions — the stretches of time a person spends on a
calibration day.

A CALIBRATION is one day (Day 1 or Day 2) of a plate: the
``CalibrationBucketSession`` row keyed by (device, date). It is complete when
every expected activity has a file — nothing about that changes here.

A WORK SESSION is a period a calibrator explicitly starts and stops in Flux2
while working on that day. One calibration can have several (someone runs out
of time and picks it up later; a second person finishes it). ``ended_at`` is
NULL while the session is open. Nothing is inferred from timers or file
counts: a session ends when a person ends it.

Why it exists: the lab TV and Axiodash can say "being calibrated now by X",
the Continue flow knows which day is genuinely mid-work, and DynamoPy can
restore the capture config it changed when the session started.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text

from axio_common.database import Base
from axio_common.utils.model_utils import current_time


WEIGHT_CLASS_HEAVY = "heavy"
WEIGHT_CLASS_LIGHT = "light"
WEIGHT_CLASSES = (WEIGHT_CLASS_HEAVY, WEIGHT_CLASS_LIGHT)


class CalibrationWorkSession(Base):
    """One start→stop stretch of work on a calibration day."""
    __tablename__ = "calibration_work_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bucket_session_id = Column(
        Integer,
        ForeignKey("calibration_bucket_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    calibrator_name = Column(String, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False, default=current_time, index=True)
    ended_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # Free text from the person ending the session, e.g. "out of time".
    notes = Column(Text, nullable=True)
    # Which app opened it ('flux2' today); room for the DAQ or a backfill.
    source = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)

    __table_args__ = (
        Index("ix_calibration_work_sessions_open", "bucket_session_id", "ended_at"),
    )


class CalibrationWorkSessionResponse(BaseModel):
    id: int
    bucket_session_id: int
    calibrator_name: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    notes: Optional[str] = None
    source: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True
