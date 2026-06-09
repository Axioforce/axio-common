"""Editable calibration procedure registry (Phase 1).

Three-level override cascade:
  CalibrationActivity (global catalog)  →  base_description, base_tags
  CalibrationFamilyActivity (per family) →  grid, duration, description_override, tags
  CalibrationActivityDayOverride (per day) → membership + description_override, tags

Day sequence for (family, day) = the family's master list filtered to the
activities that have a day-override row for that day, ordered by the
day-override order_index when set, else by the family master order_index then
reversed if that day's CalibrationFamilyDay.reverse_order is set.
Layered over axio_common.storage.activities defaults (the fallback when empty).
"""
from sqlalchemy import (
    Column, String, Text, Integer, Boolean, JSON, ForeignKey, DateTime,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import relationship

from axio_common.database import Base
from axio_common.utils.model_utils import current_time


class CalibrationActivity(Base):
    """Global activity catalog (palette). Natural key = joined activity_id."""
    __tablename__ = "calibration_activities"

    activity_id = Column(String, primary_key=True)      # 'TR-45V'
    kind = Column(String, nullable=False)               # 'train' | 'test'
    code = Column(String, nullable=False)               # '45V'
    base_description = Column(Text, nullable=True)
    base_tags = Column(JSON, nullable=True)             # list[str]
    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=current_time, onupdate=current_time)


class CalibrationFamilyActivity(Base):
    """One row per activity in a family's single master list."""
    __tablename__ = "calibration_family_activities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    family = Column(String, nullable=False, index=True)   # 'lite' | 'lp' | 'xl'
    activity_id = Column(String, ForeignKey("calibration_activities.activity_id"), nullable=False)
    order_index = Column(Integer, nullable=False, default=0)
    grid = Column(String, nullable=True)
    duration = Column(String, nullable=True)
    description_override = Column(Text, nullable=True)
    tags = Column(JSON, nullable=True)                    # list[str]
    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=current_time, onupdate=current_time)

    day_overrides = relationship(
        "CalibrationActivityDayOverride",
        back_populates="family_activity",
        cascade="all, delete-orphan",
        lazy="select",
    )

    __table_args__ = (
        UniqueConstraint("family", "activity_id", name="uq_family_activity"),
        Index("ix_family_activity_order", "family", "order_index"),
    )


class CalibrationFamilyDay(Base):
    """A day within a family's procedure, with its settings."""
    __tablename__ = "calibration_family_days"

    id = Column(Integer, primary_key=True, autoincrement=True)
    family = Column(String, nullable=False, index=True)
    day_number = Column(Integer, nullable=False)
    reverse_order = Column(Boolean, nullable=False, default=False)
    label = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=current_time, onupdate=current_time)

    __table_args__ = (
        UniqueConstraint("family", "day_number", name="uq_family_day"),
    )


class CalibrationActivityDayOverride(Base):
    """Membership (row exists => activity is in this day) + per-day overrides."""
    __tablename__ = "calibration_activity_day_overrides"

    id = Column(Integer, primary_key=True, autoincrement=True)
    family_activity_id = Column(
        Integer,
        ForeignKey("calibration_family_activities.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    day_number = Column(Integer, nullable=False)
    # Per-day capture order. NULL for families that order by the family
    # master order_index (+ reverse_order) — those resolve exactly as before.
    # Insole sets this explicitly so each day can have its own sequence.
    order_index = Column(Integer, nullable=True)
    description_override = Column(Text, nullable=True)
    tags = Column(JSON, nullable=True)                    # list[str]

    family_activity = relationship("CalibrationFamilyActivity", back_populates="day_overrides")

    __table_args__ = (
        UniqueConstraint("family_activity_id", "day_number", name="uq_activity_day"),
    )

# API request/response shapes live in axio-server/app/routers/procedures.py
# (the raw, round-trippable editor shapes), not here — the registry API
# returns the editable structure rather than a server-resolved view.
