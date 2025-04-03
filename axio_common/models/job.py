import json
import uuid

from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List

from sqlalchemy import Column, ForeignKey, JSON, text, String, Float, TEXT, DateTime, Integer, Text, BigInteger
from sqlalchemy.orm import Session

from axio_common.database import Base
from axio_common.logger import logger
from axio_common.utils.model_utils import current_time


class UpdateJobProgressRequest(BaseModel):
    job_id: str
    run_number: int
    total_runs: int
    run_config: dict
    hostname: str


class JobResponse(BaseModel):
    id: str
    device_axf_id: str
    model_type: str
    config: str
    status: str
    created_at: datetime
    updated_at: datetime
    timestamp: int
    run_number: int
    total_runs: int
    run_started_at: Optional[datetime]
    queued_at: datetime
    assigned_at: Optional[datetime]
    started_at: Optional[datetime]
    stopped_at: Optional[datetime]
    completed_at: Optional[datetime]
    last_heartbeat: Optional[datetime]
    interrupted_at: Optional[datetime]
    duration: float
    hostname: Optional[str]

    class Config:
        from_attributes = True  # Enables SQLAlchemy model compatibility


class Job(Base):
    __tablename__ = "jobs"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    device_axf_id = Column(String, ForeignKey("devices.axf_id"), nullable=False)
    model_type = Column(String, nullable=False)
    config = Column(TEXT, nullable=False)
    status = Column(String, default="queued")
    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=current_time, onupdate=current_time)

    # Individual progress fields
    timestamp = Column(BigInteger, default=0, nullable=False)
    run_number = Column(Integer, default=0, nullable=False)
    total_runs = Column(Integer, default=0, nullable=False)
    run_started_at = Column(DateTime(timezone=True), nullable=True)

    # Timestamps
    queued_at = Column(DateTime(timezone=True), nullable=False, default=current_time)
    assigned_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    stopped_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    last_heartbeat = Column(DateTime(timezone=True), nullable=True)
    interrupted_at = Column(DateTime(timezone=True), nullable=True)

    duration = Column(Float, default=0.0)
    hostname = Column(String, nullable=True)

    def update_model_type(self):
        config = json.loads(self.config)
        self.model_type = config["OUTPUT_TYPE"].split()[0]

    def update_status(self, status: str, db: Session, hostname: str = None):
        """
        Update the status of the job and log the change.
        """
        logger.info(f"Job {self.id} status updated: {self.status} -> {status}")
        self.status = status
        if status == "assigned":
            self.assigned_at = current_time()
            self.heartbeat(db)
        elif status == "completed":
            self.completed_at = current_time()
        elif status == "interrupted":
            self.interrupted_at = current_time()
            if self.hostname:
                from axio_common.utils.shared import client_by_hostname
                client = client_by_hostname(hostname, db, update_activity=False)
                client.remove_active_job(db)

        if hostname:
            logger.info(f"Job {self.id} assigned to {hostname}")
            self.hostname = hostname
        db.commit()

    def start_run(self, run_config: dict, run_number: int):
        """
        Start a new run, updating the progress and initializing run_progress.
        """
        from axio_common.models import Run
        logger.info(f"Job {self.id} starting run {run_number}")
        self.run_number = run_number
        if run_number == 1:
            self.started_at = current_time()
            self.status = "running"
            logger.info(f"Job {self.id} began first run - status updated to 'running'")

        if self.status == "assigned":
            self.status = "running"
            logger.info(f"New run starting on Job {self.id} - status updated from 'assigned' to 'running'")

        run = Run(job_id=self.id, number=run_number)
        run.initialize_from_config(run_config)
        run.is_current = True
        self.run_started_at = current_time()
        return run

    def update_progress(self, update: UpdateJobProgressRequest, db: Session):
        """
        Update the progress of the job and log the change.
        """
        from axio_common.models import Run
        logger.info(f"Job {self.id} progress updated: Run {self.run_number} -> {update.run_number}")
        self.run_number = update.run_number
        self.total_runs = update.total_runs

        # Check to see if any runs are "current"
        current_run = db.query(Run).filter(Run.job_id == self.id, Run.is_current).first()
        if current_run:
            current_run.is_current = False
            logger.info(f"Run {current_run.number} marked as not current")

        # Create a new run and set it as current
        run = self.start_run(update.run_config, update.run_number)
        db.add(run)
        db.commit()
        db.refresh(run)

    def complete_run(self, update, db):
        """
        Complete the current run and log the results.
        """
        from axio_common.models import Run
        logger.info(f"Job {self.id} run {update.run_number} completed")
        run = db.query(Run).filter(Run.job_id == self.id, Run.is_current).first()
        if not run:
            logger.error(f"No current run found for job {self.id}")
            return

        run.complete(self, update, db)

        self.duration = (run.completed_at - self.started_at).total_seconds()

        db.commit()
        return run

    def complete(self, db: Session):
        """
        Mark the job as completed and log the results.
        """
        logger.info(f"Job {self.id} completed")
        self.completed_at = current_time()
        self.duration = (self.completed_at - self.assigned_at).total_seconds()
        db.commit()

    def heartbeat(self, db: Session):
        """
        Update the timestamp of the last heartbeat.
        """
        self.last_heartbeat = current_time()
        logger.info(f"Heartbeat updated for job {self.id}")
        db.commit()

    def to_simple(self):
        """
        Return a simple version of the job for API responses.
        """
        return SimpleJob(
            id=self.id,
            device_id=self.device_axf_id,
            model_type=self.model_type,
            status=self.status,
            timestamp=self.timestamp,
            run_number=self.run_number,
            total_runs=self.total_runs,
            hostname=self.hostname,
            run_started_at=self.run_started_at,
            queued_at=self.queued_at,
            assigned_at=self.assigned_at,
            completed_at=self.completed_at,
            last_heartbeat=self.last_heartbeat,
            duration=self.duration
        )

    def to_dict(self):
        """
        Return a dictionary representation of the job.
        """
        return {
            "id": self.id,
            "device_axf_id": self.device_axf_id,
            "model_type": self.model_type,
            "config": self.config,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "timestamp": self.timestamp,
            "run_number": self.run_number,
            "total_runs": self.total_runs,
            "run_started_at": self.run_started_at,
            "queued_at": self.queued_at,
            "assigned_at": self.assigned_at,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "completed_at": self.completed_at,
            "last_heartbeat": self.last_heartbeat,
            "interrupted_at": self.interrupted_at,
            "duration": self.duration,
            "hostname": self.hostname
        }

    @classmethod
    def from_dict(cls, data):
        """
        Create a Job object from a dictionary.
        Filters out unexpected keys to prevent errors.
        """
        valid_keys = {column.name for column in cls.__table__.columns}
        filtered_data = {key: value for key, value in data.items() if key in valid_keys}
        if len(filtered_data) != len(data):
            invalid_keys = set(data) - valid_keys
            logger.warning(f"Invalid keys: {invalid_keys}")

        return cls(**filtered_data)

    def update_from_dict(self, data):
        """
        Update the Job object from a dictionary.
        Ensures `None` values don't overwrite existing values.
        """
        for key, value in data.items():
            if key in self.to_dict() and value is not None:
                setattr(self, key, value)


class SimpleJob(BaseModel):
    id: str
    device_id: str
    model_type: str
    status: str
    timestamp: int
    run_number: int
    total_runs: int
    hostname: Optional[str]
    run_started_at: Optional[datetime]
    queued_at: Optional[datetime]
    assigned_at: Optional[datetime]
    completed_at: Optional[datetime]
    last_heartbeat: Optional[datetime]
    duration: float


class JobRequest(BaseModel):
    hostname: str
    config: dict


class FetchJobRequest(BaseModel):
    hostname: str
    daemon: bool


class UpdateJobStatusRequest(BaseModel):
    job_id: str
    status: str
    timestamp: int
    hostname: str


class HeartbeatRequest(BaseModel):
    hostname: str
    job_id: str


class CompleteJobRequest(BaseModel):
    hostname: str
    job_id: str


class GetJobStatus(BaseModel):
    job_id: str
    hostname: str
    daemon: bool
