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
    """Return the canonical key for a calibrator name, or None when the input
    is empty/whitespace. Splits on common multi-name separators and uses the
    first non-empty entry as the key — `'Eric, Sky and Zach'` collapses to
    `'eric'` so the legacy free-form multi-author strings don't appear as
    fake calibrators in the rollup. Multi-attribution will move to an M2M
    junction in a follow-up; this is the conservative first cut."""
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    # Split on common multi-name separators. ' and ' is hit before ' & ' is
    # split out by the comma-or-ampersand pass so 'Eric & Zach and Sky'
    # still collapses to 'Eric'.
    for sep in (";", ","):
        if sep in s:
            s = s.split(sep)[0]
    # ' and ' / ' & ' are word-boundary separators, not bare splits — guard
    # against 'Brandon' becoming 'Br' by checking for surrounding spaces.
    lowered = s.lower()
    for sep in (" and ", " & "):
        idx = lowered.find(sep)
        if idx >= 0:
            s = s[:idx]
            lowered = s.lower()
    key = " ".join(s.split()).lower()
    return key or None


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
