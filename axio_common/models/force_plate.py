"""
Force plate assembly tracking + assembled-baseline snapshots.

Mirrors the AxioforceDynamoPy local schema (baselines.db) so the desktop app
can sync its writes to the FlyNNServer database as the source of truth for
cross-fleet drift analysis. The desktop app keeps its local SQLite for
offline operation; the server is the queryable mirror.

Design notes:
  - A force plate has four corner load cells. Corners use snake_case
    ('front_left' / 'front_right' / 'rear_left' / 'rear_right') which matches
    the operator-visible *physical* convention. The desktop applies the
    type-12 device-name remap before sending, so server data is already in
    physical-corner space.
  - assembled_at is editable with a separate audit table; initialized_at
    records first-init and is immutable.
  - assignment changes are append-only audit rows (no soft-delete on
    force_plate itself).
  - assembled_baseline rows snapshot all 8 channels at a moment in time,
    with kind='initialization' (auto-captured at chip init) or
    kind='assembled' (manual via Calibration Setup).
"""
from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel
from sqlalchemy import (
    Column, String, Text, DateTime, Integer, Float, ForeignKey, Index,
)
from sqlalchemy.orm import relationship

from axio_common.database import Base
from axio_common.utils.model_utils import current_time


CORNER_POSITIONS = ('front_left', 'front_right', 'rear_left', 'rear_right')


class ForcePlate(Base):
    __tablename__ = "force_plates"

    # device_axf_id matches Device.axf_id but isn't a FK because the desktop
    # may push a force_plate before the Device row exists in axio-server (e.g.
    # plate built today, first training job submitted next week).
    device_axf_id = Column(String, primary_key=True)
    device_type_id = Column(String, nullable=False, index=True)
    front_left_load_cell_id = Column(String, nullable=True)
    front_right_load_cell_id = Column(String, nullable=True)
    rear_left_load_cell_id = Column(String, nullable=True)
    rear_right_load_cell_id = Column(String, nullable=True)
    initialized_at = Column(DateTime(timezone=True), nullable=False)
    assembled_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=current_time, onupdate=current_time)

    assignment_history = relationship(
        "ForcePlateAssignmentHistory",
        back_populates="force_plate",
        cascade="all, delete-orphan",
        lazy="select",
    )
    assembled_date_history = relationship(
        "ForcePlateAssembledDateHistory",
        back_populates="force_plate",
        cascade="all, delete-orphan",
        lazy="select",
    )
    assembled_baselines = relationship(
        "AssembledBaseline",
        back_populates="force_plate",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def to_dict(self):
        return {
            "device_axf_id": self.device_axf_id,
            "device_type_id": self.device_type_id,
            "front_left_load_cell_id": self.front_left_load_cell_id,
            "front_right_load_cell_id": self.front_right_load_cell_id,
            "rear_left_load_cell_id": self.rear_left_load_cell_id,
            "rear_right_load_cell_id": self.rear_right_load_cell_id,
            "initialized_at": self.initialized_at,
            "assembled_at": self.assembled_at,
            "updated_at": self.updated_at,
        }


class ForcePlateAssignmentHistory(Base):
    __tablename__ = "force_plate_assignment_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_axf_id = Column(String, ForeignKey("force_plates.device_axf_id"), nullable=False, index=True)
    position = Column(String, nullable=False)
    load_cell_id = Column(String, nullable=True, index=True)
    previous_load_cell_id = Column(String, nullable=True)
    changed_at = Column(DateTime(timezone=True), nullable=False)
    change_type = Column(String, nullable=False)  # 'initialize' | 'reassign' | 'remove'

    force_plate = relationship("ForcePlate", back_populates="assignment_history")

    def to_dict(self):
        return {
            "id": self.id,
            "device_axf_id": self.device_axf_id,
            "position": self.position,
            "load_cell_id": self.load_cell_id,
            "previous_load_cell_id": self.previous_load_cell_id,
            "changed_at": self.changed_at,
            "change_type": self.change_type,
        }


class ForcePlateAssembledDateHistory(Base):
    __tablename__ = "force_plate_assembled_date_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_axf_id = Column(String, ForeignKey("force_plates.device_axf_id"), nullable=False, index=True)
    assembled_at = Column(DateTime(timezone=True), nullable=False)
    previous_assembled_at = Column(DateTime(timezone=True), nullable=True)
    changed_at = Column(DateTime(timezone=True), nullable=False)
    reason = Column(Text, nullable=True)

    force_plate = relationship("ForcePlate", back_populates="assembled_date_history")

    def to_dict(self):
        return {
            "id": self.id,
            "device_axf_id": self.device_axf_id,
            "assembled_at": self.assembled_at,
            "previous_assembled_at": self.previous_assembled_at,
            "changed_at": self.changed_at,
            "reason": self.reason,
        }


# ----- Pydantic request/response shapes -----


class ForcePlateRequest(BaseModel):
    device_axf_id: str
    device_type_id: str
    front_left_load_cell_id: Optional[str] = None
    front_right_load_cell_id: Optional[str] = None
    rear_left_load_cell_id: Optional[str] = None
    rear_right_load_cell_id: Optional[str] = None
    initialized_at: Optional[datetime] = None
    assembled_at: Optional[datetime] = None


class ForcePlateUpdateRequest(BaseModel):
    """Partial corner update with optional swap reasons keyed by corner position.
    Reasons get attached to the outgoing load cell as 'swap-out' notes."""
    front_left_load_cell_id: Optional[str] = None
    front_right_load_cell_id: Optional[str] = None
    rear_left_load_cell_id: Optional[str] = None
    rear_right_load_cell_id: Optional[str] = None
    swap_reasons: Optional[dict] = None
    changed_at: Optional[datetime] = None


class ForcePlateAssembledDateUpdateRequest(BaseModel):
    assembled_at: datetime
    reason: Optional[str] = None


class ForcePlateResponse(BaseModel):
    device_axf_id: str
    device_type_id: str
    front_left_load_cell_id: Optional[str]
    front_right_load_cell_id: Optional[str]
    rear_left_load_cell_id: Optional[str]
    rear_right_load_cell_id: Optional[str]
    initialized_at: datetime
    assembled_at: Optional[datetime]
    updated_at: datetime

    class Config:
        from_attributes = True
