"""
Calibration session tracking — one row per *job submission* (not per date).
A session can span multiple session_dates and multiple calibrators; both ride
along as child tables. The session row is linked to the Job created from the
same Neuralizer submission, so the dashboard can pivot from a job back to the
calibration data it was trained on.

Calibrator names are stored in Title Case so a later submission spelled
differently ('SKY' vs 'sky' vs 'Sky') doesn't proliferate duplicates.

Status reflects the live-test verdict for the model trained on the session:
'untested' (default; freshly submitted, awaiting live testing), 'pass'
(model passed live testing), 'fail' (model failed). The live-testing
software flips the status via PATCH /calibration-sessions/{id}/status with
an optional reason captured in `status_reason`.
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


CALIBRATION_SESSION_STATUSES = ('untested', 'pass', 'fail')


class CalibrationSession(Base):
    __tablename__ = "calibration_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Nullable on legacy rows whose source job was purged before backfill.
    job_id = Column(String, ForeignKey("jobs.id"), nullable=True, index=True)
    device_axf_id = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default='untested')  # 'untested' | 'pass' | 'fail'
    status_reason = Column(Text, nullable=True)
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
            "status_reason": self.status_reason,
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
    with all dates listed under `dates`. New sessions land as 'untested';
    live-testing software flips status via the status endpoint."""
    device_axf_id: str
    job_id: Optional[str] = None
    dates: List[CalibrationSessionDatePayload] = []
    calibrators: List[str] = []
    notes: Optional[str] = None


class CalibrationSessionStatusUpdate(BaseModel):
    """Request body for PATCH /calibration-sessions/{id}/status.
    Sent by the live-testing software once a model has been evaluated."""
    status: str  # 'untested' | 'pass' | 'fail'
    reason: Optional[str] = None


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
    status_reason: Optional[str]
    notes: Optional[str]
    created_at: datetime
    dates: List[CalibrationSessionDateResponse] = []
    calibrators: List[str] = []

    class Config:
        from_attributes = True


class CalibratorAnalytics(BaseModel):
    """Per-calibrator analytics derived from the session/calibrator junction.
    Returned by GET /force-plate/calibrators."""
    name: str
    session_count: int
    plates: List[str]
    pass_count: int
    fail_count: int
    untested_count: int
