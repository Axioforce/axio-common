"""
Calibration session tracking — one row per *job submission* (not per date).
A session can span multiple session_dates and multiple calibrators; both ride
along as child tables. The session row is linked to the Job created from the
same Neuralizer submission, so the dashboard can pivot from a job back to the
calibration data it was trained on.

Calibrator names are stored in Title Case so a later submission spelled
differently ('SKY' vs 'sky' vs 'Sky') doesn't proliferate duplicates.

Discarded sessions get an explicit row with status='discarded' + reason via a
small "Mark session discarded" sub-flow on Flux2 Calibration Setup; that's the
only operator-facing input. Sessions that were tossed before any job was
submitted simply never become rows. Discard rows always carry exactly one date
child (the date being discarded).
"""
from datetime import datetime, date
from typing import Optional, List

from pydantic import BaseModel
from sqlalchemy import (
    Column, String, Text, DateTime, Integer, ForeignKey, Date,
)
from sqlalchemy.orm import relationship

from axio_common.database import Base
from axio_common.utils.model_utils import current_time


CALIBRATION_SESSION_STATUSES = ('used', 'discarded')


class CalibrationSession(Base):
    __tablename__ = "calibration_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Nullable: discarded sessions never had a successful job submission.
    job_id = Column(String, ForeignKey("jobs.id"), nullable=True, index=True)
    device_axf_id = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default='used')  # 'used' | 'discarded'
    discard_reason = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)

    dates = relationship(
        "CalibrationSessionDate",
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="CalibrationSessionDate.session_date",
    )
    calibrators = relationship(
        "CalibrationSessionCalibrator",
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "job_id": self.job_id,
            "device_axf_id": self.device_axf_id,
            "status": self.status,
            "discard_reason": self.discard_reason,
            "notes": self.notes,
            "created_at": self.created_at,
            "dates": [d.to_dict() for d in (self.dates or [])],
            "calibrators": [c.calibrator_name for c in (self.calibrators or [])],
        }


class CalibrationSessionDate(Base):
    __tablename__ = "calibration_session_dates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    calibration_session_id = Column(
        Integer, ForeignKey("calibration_sessions.id"), nullable=False, index=True,
    )
    session_date = Column(Date, nullable=False, index=True)
    calibration_directory = Column(String, nullable=True)

    session = relationship("CalibrationSession", back_populates="dates")

    def to_dict(self):
        return {
            "session_date": self.session_date,
            "calibration_directory": self.calibration_directory,
        }


class CalibrationSessionCalibrator(Base):
    __tablename__ = "calibration_session_calibrators"

    id = Column(Integer, primary_key=True, autoincrement=True)
    calibration_session_id = Column(Integer, ForeignKey("calibration_sessions.id"), nullable=False, index=True)
    calibrator_name = Column(String, nullable=False)

    session = relationship("CalibrationSession", back_populates="calibrators")


# ----- Pydantic request/response shapes -----


class CalibrationSessionDatePayload(BaseModel):
    session_date: date
    calibration_directory: Optional[str] = None


class CalibrationSessionPayload(BaseModel):
    """Single session entry shipped from Neuralizer's submit_job_to_server.
    A submission spanning multiple date directories collapses into ONE payload
    with all dates listed under `dates`."""
    device_axf_id: str
    job_id: Optional[str] = None
    dates: List[CalibrationSessionDatePayload] = []
    calibrators: List[str] = []
    notes: Optional[str] = None


class CalibrationSessionDiscardRequest(BaseModel):
    """Sent by Flux2 Calibration Setup when an operator marks a failed session."""
    device_axf_id: str
    session_date: date
    discard_reason: str
    calibration_directory: Optional[str] = None


class CalibrationSessionDateResponse(BaseModel):
    session_date: date
    calibration_directory: Optional[str]

    class Config:
        from_attributes = True


class CalibrationSessionResponse(BaseModel):
    id: int
    job_id: Optional[str]
    device_axf_id: str
    status: str
    discard_reason: Optional[str]
    notes: Optional[str]
    created_at: datetime
    dates: List[CalibrationSessionDateResponse] = []
    calibrators: List[str] = []

    class Config:
        from_attributes = True
