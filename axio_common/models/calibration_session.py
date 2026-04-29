"""
Calibration session tracking — one row per (force plate, session date),
populated automatically from `tests.txt` files when AxioforceNeuralizer
submits a training job. Calibrators ride along as a child table.

Discarded sessions get an explicit row with status='discarded' + reason via a
small "Mark session discarded" sub-flow on Flux2 Calibration Setup; that's the
only operator-facing input. Sessions that were tossed before any job was
submitted simply never become rows.

No unique constraint on (device_axf_id, session_date) so a discarded session
followed by a successful re-attempt on the same date can both be preserved.
"""
from datetime import datetime, date
from typing import Optional, List

from pydantic import BaseModel
from sqlalchemy import (
    Column, String, Text, DateTime, Integer, ForeignKey, Date, Index,
)
from sqlalchemy.orm import relationship

from axio_common.database import Base
from axio_common.utils.model_utils import current_time


CALIBRATION_SESSION_STATUSES = ('used', 'discarded')


class CalibrationSession(Base):
    __tablename__ = "calibration_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_axf_id = Column(String, nullable=False, index=True)
    session_date = Column(Date, nullable=False, index=True)
    calibration_directory = Column(String, nullable=True)
    status = Column(String, nullable=False, default='used')  # 'used' | 'discarded'
    discard_reason = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)

    calibrators = relationship(
        "CalibrationSessionCalibrator",
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "device_axf_id": self.device_axf_id,
            "session_date": self.session_date,
            "calibration_directory": self.calibration_directory,
            "status": self.status,
            "discard_reason": self.discard_reason,
            "notes": self.notes,
            "created_at": self.created_at,
            "calibrators": [c.calibrator_name for c in (self.calibrators or [])],
        }


class CalibrationSessionCalibrator(Base):
    __tablename__ = "calibration_session_calibrators"

    id = Column(Integer, primary_key=True, autoincrement=True)
    calibration_session_id = Column(Integer, ForeignKey("calibration_sessions.id"), nullable=False, index=True)
    calibrator_name = Column(String, nullable=False)

    session = relationship("CalibrationSession", back_populates="calibrators")


# ----- Pydantic request/response shapes -----


class CalibrationSessionPayload(BaseModel):
    """Single session entry shipped from Neuralizer's submit_job_to_server.
    Multiple of these can ride in one job submission."""
    device_axf_id: str
    session_date: date
    calibration_directory: Optional[str] = None
    calibrators: List[str] = []
    notes: Optional[str] = None


class CalibrationSessionDiscardRequest(BaseModel):
    """Sent by Flux2 Calibration Setup when an operator marks a failed session."""
    device_axf_id: str
    session_date: date
    discard_reason: str
    calibration_directory: Optional[str] = None


class CalibrationSessionResponse(BaseModel):
    id: int
    device_axf_id: str
    session_date: date
    calibration_directory: Optional[str]
    status: str
    discard_reason: Optional[str]
    notes: Optional[str]
    created_at: datetime
    calibrators: List[str] = []

    class Config:
        from_attributes = True
