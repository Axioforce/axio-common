"""
Per-plate baseline snapshots — full-plate "all 8 channels at zero load" captures
taken at chip initialization (kind='initialization_assembled' or
'initialization_unassembled') and post-assembly (kind='assembled') from
Calibration Setup. Pairs with ForcePlate so drift between manufacturing ->
initialization -> assembled lifecycle stages is queryable per load cell.

The two initialization kinds split on whether the load cells were already
mechanically mounted in the plate at the time of chip-init: assembled
resembles 'assembled' captures; unassembled is closer to a manufacturing
baseline since each load cell is in free state. (Legacy rows tagged
'initialization' were renamed to 'initialization_assembled' in migration
b1d2a8e3f9c0 — they pre-date the unassembled flow.)

Per-channel std_x/y/z come from the desktop's tare-samples accumulator and
distinguish real signal drift from sensor noise. firmware_version + JSON
config_snapshot let us correlate captures with firmware/config changes.
"""
from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel
from sqlalchemy import (
    Column, String, Text, DateTime, Integer, Float, ForeignKey, JSON, Index,
)
from sqlalchemy.orm import relationship

from axio_common.database import Base
from axio_common.utils.model_utils import current_time


ASSEMBLED_BASELINE_KINDS = (
    'initialization_unassembled',
    'initialization_assembled',
    'assembled',
)


class AssembledBaseline(Base):
    __tablename__ = "assembled_baselines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_axf_id = Column(String, ForeignKey("force_plates.device_axf_id"), nullable=False, index=True)
    device_type_id = Column(String, nullable=False)
    # 'initialization_unassembled' | 'initialization_assembled' | 'assembled'
    kind = Column(String, nullable=False)
    captured_at = Column(DateTime(timezone=True), nullable=False, index=True)
    firmware_version = Column(String, nullable=True)
    # Snapshot of relevant dynamo_config values at capture time. Lets us answer
    # "did the firmware or config change between these two captures?" without
    # cross-referencing external logs.
    config_snapshot = Column(JSON, nullable=True)

    force_plate = relationship("ForcePlate", back_populates="assembled_baselines")
    sensors = relationship(
        "AssembledBaselineSensor",
        back_populates="baseline",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "device_axf_id": self.device_axf_id,
            "device_type_id": self.device_type_id,
            "kind": self.kind,
            "captured_at": self.captured_at,
            "firmware_version": self.firmware_version,
            "config_snapshot": self.config_snapshot,
            "sensors": [s.to_dict() for s in (self.sensors or [])],
        }


class AssembledBaselineSensor(Base):
    __tablename__ = "assembled_baseline_sensors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    assembled_baseline_id = Column(Integer, ForeignKey("assembled_baselines.id"), nullable=False, index=True)
    sensor_index = Column(Integer, nullable=False)        # device's data-stream index
    sensor_order = Column(Integer, nullable=True)         # sensor.order (also data-stream order)
    sensor_name = Column(String, nullable=True)           # raw device name, kept for audit
    corner_position = Column(String, nullable=True)       # AFTER type-12 remap
    inner_outer = Column(String, nullable=True)           # 'inner' | 'outer'
    load_cell_id = Column(String, nullable=True, index=True)
    x = Column(Float, nullable=False)
    y = Column(Float, nullable=False)
    z = Column(Float, nullable=False)
    std_x = Column(Float, nullable=True)
    std_y = Column(Float, nullable=True)
    std_z = Column(Float, nullable=True)
    temp = Column(Float, nullable=True)

    baseline = relationship("AssembledBaseline", back_populates="sensors")

    def to_dict(self):
        return {
            "id": self.id,
            "assembled_baseline_id": self.assembled_baseline_id,
            "sensor_index": self.sensor_index,
            "sensor_order": self.sensor_order,
            "sensor_name": self.sensor_name,
            "corner_position": self.corner_position,
            "inner_outer": self.inner_outer,
            "load_cell_id": self.load_cell_id,
            "x": self.x, "y": self.y, "z": self.z,
            "std_x": self.std_x, "std_y": self.std_y, "std_z": self.std_z,
            "temp": self.temp,
        }


# ----- Pydantic request/response shapes -----


class AssembledBaselineSensorPayload(BaseModel):
    sensor_index: int
    sensor_order: Optional[int] = None
    sensor_name: Optional[str] = None
    corner_position: Optional[str] = None
    inner_outer: Optional[str] = None
    load_cell_id: Optional[str] = None
    x: float
    y: float
    z: float
    std_x: Optional[float] = None
    std_y: Optional[float] = None
    std_z: Optional[float] = None
    temp: Optional[float] = None


class AssembledBaselineRequest(BaseModel):
    device_axf_id: str
    device_type_id: str
    kind: str
    captured_at: datetime
    firmware_version: Optional[str] = None
    config_snapshot: Optional[dict] = None
    sensors: List[AssembledBaselineSensorPayload]


class AssembledBaselineResponse(BaseModel):
    id: int
    device_axf_id: str
    device_type_id: str
    kind: str
    captured_at: datetime
    firmware_version: Optional[str]
    config_snapshot: Optional[dict]
    sensors: List[dict]

    class Config:
        from_attributes = True
