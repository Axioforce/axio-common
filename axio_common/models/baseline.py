"""
Manufacturing / per-load-cell baselines — the lifecycle stage that comes
BEFORE a load cell is assembled into a force plate. Mirrors the
AxioforceDynamoPy local `baseline` + `baseline_sensor` tables.

Each baseline row represents one capture event for one load cell at a single
moment, with N sensor rows (typically 2 — inner + outer channels). The
`device_id` field is the DEVICE THAT CAPTURED the baseline (e.g. the
diagnostic kit "11.00000004" or the sentinel "master-list-backfill" for
historically imported data) — it is NOT a force plate device id, hence no
FK to `force_plates`.

The trio (load_cell_id, captured_at, device_id) is unique so the desktop can
push the same baseline repeatedly without duplicating server rows. Useful
for re-running the bulk resync and for offline-then-catch-up flows.
"""
from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel
from sqlalchemy import (
    Column, String, DateTime, Integer, Float, ForeignKey, Index, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from axio_common.database import Base
from axio_common.utils.model_utils import current_time


class Baseline(Base):
    __tablename__ = "baselines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    load_cell_id = Column(String, ForeignKey("load_cells.load_cell_id"),
                          nullable=False, index=True)
    # Manufacturing/diagnostic-kit device id, NOT a force plate axf_id.
    # Common values: "11.00000004" (diagnostic kit) or "master-list-backfill".
    device_id = Column(String, nullable=False)
    captured_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)

    sensors = relationship(
        "BaselineSensor", back_populates="baseline",
        cascade="all, delete-orphan", lazy="select",
    )

    __table_args__ = (
        # Idempotency for desktop pushes — re-pushing the same capture is a no-op
        # rather than duplicating rows. The desktop's local DB doesn't enforce
        # this, but server-side dedup is cheap and sane.
        UniqueConstraint('load_cell_id', 'captured_at', 'device_id', name='uq_baseline_dedup'),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "load_cell_id": self.load_cell_id,
            "device_id": self.device_id,
            "captured_at": self.captured_at,
            "created_at": self.created_at,
            "sensors": [s.to_dict() for s in (self.sensors or [])],
        }


class BaselineSensor(Base):
    __tablename__ = "baseline_sensors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    baseline_id = Column(Integer, ForeignKey("baselines.id"), nullable=False, index=True)
    sensor_index = Column(Integer, nullable=False)
    x = Column(Float, nullable=False)
    y = Column(Float, nullable=False)
    z = Column(Float, nullable=False)
    temp = Column(Float, nullable=True)
    position = Column(String, nullable=True)  # 'inner' | 'outer'

    baseline = relationship("Baseline", back_populates="sensors")

    def to_dict(self):
        return {
            "id": self.id,
            "baseline_id": self.baseline_id,
            "sensor_index": self.sensor_index,
            "x": self.x, "y": self.y, "z": self.z,
            "temp": self.temp,
            "position": self.position,
        }


# ----- Pydantic request/response shapes -----


class BaselineSensorPayload(BaseModel):
    sensor_index: int
    x: float
    y: float
    z: float
    temp: Optional[float] = None
    position: Optional[str] = None


class BaselineRequest(BaseModel):
    load_cell_id: str
    device_id: str
    captured_at: datetime
    sensors: List[BaselineSensorPayload]


class BaselineResponse(BaseModel):
    id: int
    load_cell_id: str
    device_id: str
    captured_at: datetime
    created_at: datetime
    sensors: List[dict]

    class Config:
        from_attributes = True
