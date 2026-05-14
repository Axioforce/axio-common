"""
Job ↔ CalibrationBucketSession M2M link.

One row per (job, bucket_session) reference, populated by the bucket sync
that walks Job.config (SELECTED_SESSIONS / TRAIN_INPUT_DIR / TEST_INPUT_DIR)
and points each match at the CBS row it consumed. Lets the dashboard answer
"which sessions did this job train on?" and "which jobs trained on this
session?" without re-doing the substring match per request.

`kind` records which list the reference came from so the UI can distinguish
training inputs from test inputs:
  - 'train' — appeared in TRAIN_INPUT_DIR (legacy) or SELECTED_SESSIONS as train
  - 'test'  — appeared in TEST_INPUT_DIR
  - 'both'  — referenced from both lists in the same job
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, Index,
)
from sqlalchemy.orm import relationship

from axio_common.database import Base
from axio_common.utils.model_utils import current_time


KIND_TRAIN_LINK = "train"
KIND_TEST_LINK = "test"
KIND_BOTH_LINK = "both"


class JobBucketSession(Base):
    """Link row between a Job and a CalibrationBucketSession the job consumed.
    Inserted by the bucket sync; never edited by hand."""
    __tablename__ = "job_bucket_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(
        String, ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    bucket_session_id = Column(
        Integer, ForeignKey("calibration_bucket_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    kind = Column(String, nullable=False, default=KIND_TRAIN_LINK)

    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)

    job = relationship("Job", lazy="select")
    bucket_session = relationship("CalibrationBucketSession", lazy="select")

    __table_args__ = (
        UniqueConstraint(
            "job_id", "bucket_session_id",
            name="uq_job_bucket_sessions_job_session",
        ),
        Index("ix_job_bucket_sessions_session", "bucket_session_id"),
    )


class JobBucketSessionResponse(BaseModel):
    id: int
    job_id: str
    bucket_session_id: int
    kind: str
    created_at: datetime

    class Config:
        from_attributes = True
