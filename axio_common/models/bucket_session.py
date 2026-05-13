"""
Bucket-backed calibration sessions.

A row per (device, date) capture session that lives in the Tigris bucket.
Maintained by a periodic sync task in axio-server (which walks the bucket
and upserts these rows). The Live Sessions dashboard reads from here so it
doesn't pay for a Tigris scan on every page poll.

Distinct from `CalibrationSession` in calibration_session.py — that model
represents a *training-job submission* (one user-initiated training run
can span multiple bucket sessions on different days). The link will be
formalized in a follow-up; for now they coexist.
"""
from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel
from sqlalchemy import (
    Column, ForeignKey, Index, Integer, String, DateTime, Float, Boolean,
    JSON, BigInteger, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from axio_common.database import Base
from axio_common.utils.model_utils import current_time


# Activity kinds — matches axio_common.storage.activities.ACTIVITIES keys.
KIND_TRAIN = "train"
KIND_TEST = "test"
KIND_OTHER = "other"      # tests.txt or anything not under train/test/


class CalibrationBucketSession(Base):
    """One row per (device, date) bucket session — the unit a calibrator
    captures in a day."""
    __tablename__ = "calibration_bucket_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_axf_id = Column(String, ForeignKey("devices.axf_id"), nullable=False, index=True)
    # ISO date string from the bucket path (e.g. "2026-05-13"). Kept as
    # string to mirror exactly what's in the path; date-typed comparisons
    # are lex-correct because ISO dates sort.
    date_iso = Column(String, nullable=False, index=True)
    type_id = Column(String, nullable=False, index=True)
    # Calibration family: 'lite' | 'lp' | 'xl' | None when type isn't mapped.
    family = Column(String, nullable=True)
    # 1-based index within this plate's recent sessions (recomputed on each sync).
    session_number = Column(Integer, nullable=False, default=1)
    # Full bucket prefix, e.g. "10/10-00000002/2026-05-13/". Stored so the
    # dashboard can hand it back without re-deriving.
    bucket_prefix = Column(String, nullable=False)

    # When the tests.txt object landed (= session-started clock).
    tests_txt_uploaded_at = Column(DateTime(timezone=True), nullable=True)
    # Newest LastModified across this session's non-hidden files.
    last_upload_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_upload_key = Column(String, nullable=True)

    total_files = Column(Integer, nullable=False, default=0)
    total_bytes = Column(BigInteger, nullable=False, default=0)
    # Active capture seconds: sum of inter-upload deltas <=30 min.
    duration_seconds = Column(Float, nullable=False, default=0.0)

    # Parsed from tests.txt. Stored denormalized + raw for queries / debug.
    calibrator_name = Column(String, nullable=True, index=True)
    location = Column(String, nullable=True, index=True)
    tests_txt_fields = Column(JSON, nullable=True)

    # Admin flags. Replaces _config/admin_state.json's session_flags.
    flag_complete = Column(Boolean, nullable=False, default=False)
    flag_soft_deleted = Column(Boolean, nullable=False, default=False)
    # True when an admin has set a per-(family, session_number) override.
    # Resolved at API time, not stored on this row.
    is_overridden = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=current_time, onupdate=current_time)

    # Relationships
    files = relationship(
        "CalibrationBucketFile",
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="select",
    )
    device = relationship("Device", lazy="joined")

    __table_args__ = (
        UniqueConstraint("device_axf_id", "date_iso", name="uq_bucket_session_device_date"),
        Index("ix_bucket_session_active", "device_axf_id", "last_upload_at"),
    )


class CalibrationBucketFile(Base):
    """One row per object in the bucket under a CalibrationBucketSession.

    activity_id stores the bare activity code (e.g. 'BER', 'STK1'); the
    'TR-'/'TE-' part is captured in `kind` so callers don't have to parse
    it back out. For non-activity files (tests.txt, raw_data/*, etc.)
    activity_id is null and kind='other'."""
    __tablename__ = "calibration_bucket_files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bucket_session_id = Column(
        Integer,
        ForeignKey("calibration_bucket_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    bucket_key = Column(String, nullable=False, unique=True, index=True)
    filename = Column(String, nullable=False)
    activity_id = Column(String, nullable=True, index=True)   # 'BER', 'STK1', etc.
    kind = Column(String, nullable=False, default=KIND_OTHER)  # 'train' | 'test' | 'other'

    last_modified_at = Column(DateTime(timezone=True), nullable=True, index=True)
    size = Column(BigInteger, nullable=False, default=0)

    # Soft-delete (file-level). Hidden files don't count toward session
    # progress/duration but stay in Tigris.
    is_hidden = Column(Boolean, nullable=False, default=False, index=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=current_time, onupdate=current_time)

    session = relationship("CalibrationBucketSession", back_populates="files")

    __table_args__ = (
        Index("ix_bucket_file_session_kind", "bucket_session_id", "kind"),
    )


# -------- Pydantic response shapes -------

class CalibrationBucketFileResponse(BaseModel):
    id: int
    bucket_key: str
    filename: str
    activity_id: Optional[str] = None
    kind: str
    last_modified_at: Optional[datetime] = None
    size: int = 0
    is_hidden: bool = False

    class Config:
        from_attributes = True


class CalibrationBucketSessionResponse(BaseModel):
    id: int
    device_axf_id: str
    date_iso: str
    type_id: str
    family: Optional[str] = None
    session_number: int
    bucket_prefix: str
    tests_txt_uploaded_at: Optional[datetime] = None
    last_upload_at: Optional[datetime] = None
    last_upload_key: Optional[str] = None
    total_files: int = 0
    total_bytes: int = 0
    duration_seconds: float = 0.0
    calibrator_name: Optional[str] = None
    location: Optional[str] = None
    tests_txt_fields: Optional[dict] = None
    flag_complete: bool = False
    flag_soft_deleted: bool = False
    is_overridden: bool = False

    class Config:
        from_attributes = True
