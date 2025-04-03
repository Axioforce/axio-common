import uuid

from pydantic import BaseModel
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, Float, Integer
from sqlalchemy.orm import Session
from typing import Optional, Union, List

from database import Base
from logger_config import logger
from utils.model_utils import current_time


class ClientResponse(BaseModel):
    id: str
    hostname: Optional[str]
    ip_address: str
    daemon: bool
    status: str
    created_at: datetime
    updated_at: datetime
    completed_jobs: int
    total_duration: float
    active_jobs: int
    submitted_jobs: int

    class Config:
        from_attributes = True  # Enables SQLAlchemy model compatibility


class Client(Base):
    __tablename__ = "clients"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    hostname = Column(String, nullable=True)
    ip_address = Column(String, nullable=False)
    daemon = Column(Boolean, nullable=False, default=True)
    status = Column(String, nullable=False, default="active")
    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=current_time, onupdate=current_time)
    completed_jobs = Column(Integer, default=0)
    total_duration = Column(Float, default=0.0)
    active_jobs = Column(Integer, default=0)
    submitted_jobs = Column(Integer, default=0)

    def __init__(self, hostname: Optional[str], ip_address: str, daemon: bool):
        """
        Initialize a new client object.
        """
        super().__init__(hostname=hostname, ip_address=ip_address, daemon=daemon)

    def update_hostname(self, hostname: str, db: Session):
        """
        Update the hostname of the client.
        """
        if self.hostname != hostname:
            logger.info(f"Updating hostname for client {self.hostname} -> {hostname}")
            self.hostname = hostname

    def update_ip(self, ip_address: str, db: Session):
        """
        Update the IP address of the client.
        """
        if self.ip_address != ip_address:
            logger.info(f"Updating IP address for client {self.hostname} -> {ip_address}")
            self.ip_address = ip_address
            db.commit()

    def mark_active(self, db: Session):
        """
        Mark the client as active.
        """
        prev_status = self.status
        self.status = "active"
        logger.info(f"Marking status for client {self.hostname}: {prev_status} -> {self.status}")
        db.commit()

    def mark_inactive(self, db: Session):
        """
        Mark the client as inactive.
        """
        prev_status = self.status
        self.status = "inactive"
        logger.info(f"Marking status for client {self.hostname}: {prev_status} -> {self.status}")
        db.commit()

    def update_status(self, status: str, db: Session):
        """
        Update the status of the client.
        """
        if self.status != status:
            logger.info(f"Updating status for client {self.hostname} -> {status}")
            self.status = status
            db.commit()

    def update_daemon(self, daemon: bool, db: Session):
        """
        Update the daemon status of the client.
        """
        if self.daemon != daemon:
            logger.info(f"Updating daemon status for client {self.hostname} -> {daemon}")
            self.daemon = daemon
            db.commit()

    def shutdown_daemon(self, db: Session):
        """
        Mark the client daemon as shutting down.
        """
        logger.info(f"Shutting down daemon for client {self.hostname}")
        self.status = "shutting_down"
        db.commit()

    def shutdown_job(self, job_id: str, db: Session):
        """
        Shutdown a specific job on the client and log the action.
        """
        from utils.shared import update_job_status
        logger.info(f"Job {job_id} on {self.hostname} is shutting down.")
        update_job_status(job_id, "shutting_down", db)

    def update_job_tracking(self, new_job: bool, duration: float, db: Session):
        if new_job:
            self.active_jobs += 1
        else:
            self.total_duration += duration
        db.commit()

    def complete_job(self, db: Session):
        self.active_jobs = max(0, self.active_jobs - 1)
        self.completed_jobs +=1
        db.commit()

    def remove_active_job(self, db: Session):
        self.active_jobs = max(0, self.active_jobs - 1)
        db.commit()

    def to_dict(self):
        return {
            "id": self.id,
            "hostname": self.hostname,
            "ip_address": self.ip_address,
            "daemon": self.daemon,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_jobs": self.completed_jobs,
            "total_duration": self.total_duration,
            "active_jobs": self.active_jobs,
            "submitted_jobs": self.submitted_jobs
        }

    @classmethod
    def from_dict(cls, data):
        """
        Create a Client object from a dictionary.
        Filters out unexpected keys to prevent errors.
        """
        valid_keys = {column.name for column in cls.__table__.columns}
        filtered_data = {key: value for key, value in data.items() if key in valid_keys}

        # Extract initialization data
        hostname = filtered_data.pop("hostname", None)
        ip_address = filtered_data.pop("ip_address", None)
        daemon = filtered_data.pop("daemon", None)

        # Create the Client object
        try:
            client = cls(hostname, ip_address, daemon)
            client.update_from_dict(filtered_data)
        except Exception as e:
            logger.error(f"Error creating client from dict: {e}")
            return None

        return client

    def update_from_dict(self, data):
        """
        Update the Client object from a dictionary.
        Ensures `None` values don't overwrite existing values.
        """
        for key, value in data.items():
            if key in self.to_dict() and value is not None:
                setattr(self, key, value)


class ClientRequest(BaseModel):
    hostname: str
    daemon: Optional[bool] = True


class UpdateClientStatusRequest(BaseModel):
    status: str
    hostname: str


class ClientShutdownRequest(BaseModel):
    hostname: Optional[str] = None
    target: Union[str, List[str]]


class ShutdownResponse(BaseModel):
    shutdown: bool
    status: Optional[str] = None
