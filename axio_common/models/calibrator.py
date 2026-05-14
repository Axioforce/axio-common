"""
Calibrator identity table.

Promotes the free-form `calibrator_name` string on CalibrationBucketSession
into a real entity with deduplication, optional metadata, and a foreign key
back from CBS rows. Names from tests.txt come in mixed casing ('Matt', 'matt',
'sky', etc.); we canonicalize on a lowercase `name_key` for uniqueness while
preserving the first-seen casing in `display_name` for the dashboard.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import (
    Column, DateTime, Integer, String, Text, Boolean,
)
from sqlalchemy.orm import relationship

from axio_common.database import Base
from axio_common.utils.model_utils import current_time


class Calibrator(Base):
    """One row per distinct person who appears as calibrated_by in tests.txt.
    Upserted by the bucket sync; CalibrationBucketSession.calibrator_id points
    here so we have a stable identity for analytics + future metadata
    (specialty, contact, certification, etc.)."""
    __tablename__ = "calibrators"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Lowercased + stripped form used for uniqueness ('matt', 'sky', etc.).
    name_key = Column(String, nullable=False, unique=True, index=True)
    # First-seen casing from the bucket. Display surface; not unique.
    display_name = Column(String, nullable=False)
    # Editorial; not auto-populated. Free-form notes about the person.
    notes = Column(Text, nullable=True)
    # Soft flag so retired/inactive calibrators can be hidden from default
    # views without losing historical attribution.
    active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)
    updated_at = Column(
        DateTime(timezone=True), nullable=False,
        default=current_time, onupdate=current_time,
    )

    bucket_sessions = relationship(
        "CalibrationBucketSession",
        back_populates="calibrator",
        lazy="select",
    )


def normalize_calibrator_name(raw: Optional[str]) -> Optional[str]:
    """Return the lowercase + collapsed-whitespace form of a calibrator name,
    or None if the input is empty/whitespace. Used for name_key lookups."""
    if not raw:
        return None
    s = " ".join(raw.split()).lower()
    return s or None


class CalibratorResponse(BaseModel):
    id: int
    name_key: str
    display_name: str
    notes: Optional[str] = None
    active: bool = True
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
