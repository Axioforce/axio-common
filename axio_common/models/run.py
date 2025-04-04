import json
import uuid
from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel
from sqlalchemy import Column, String, ForeignKey, Integer, Float, DateTime, Boolean, JSON
from sqlalchemy.orm import Session

from axio_common.database import Base
from axio_common.logger import logger
from axio_common.utils.model_utils import current_time


class CompleteRunRequest(BaseModel):
    job_id: str
    run_number: int
    train_metrics: dict
    val_metrics: dict
    test_metrics: dict
    epochs_completed: int
    hostname: str


class RunResponse(BaseModel):
    id: str
    job_id: str
    number: int
    created_at: datetime
    updated_at: datetime
    duration: float
    completed_at: Optional[datetime]
    learning_rate: Optional[float]
    activation: Optional[str]
    optimizer: Optional[str]
    layers: Optional[List[int]]
    batch_size: Optional[int]
    epochs: Optional[int]
    epochs_completed: int
    is_best: bool
    is_current: bool
    train_metrics: Optional[dict]
    val_metrics: Optional[dict]
    test_metrics: Optional[dict]

    class Config:
        from_attributes = True  # Enables SQLAlchemy model compatibility


class Run(Base):
    __tablename__ = "runs"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id = Column(String, ForeignKey("jobs.id"), nullable=False)
    number = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=current_time, onupdate=current_time)
    duration = Column(Float, default=0.0)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    learning_rate = Column(Float, nullable=True)
    activation = Column(String, nullable=True)
    optimizer = Column(String, nullable=True)
    layers = Column(JSON, nullable=True)
    batch_size = Column(Integer, nullable=True)
    epochs = Column(Integer, nullable=True)
    epochs_completed = Column(Integer, default=0)
    is_best = Column(Boolean, default=False)
    is_current = Column(Boolean, default=False)
    train_metrics = Column(JSON, nullable=True)
    val_metrics = Column(JSON, nullable=True)
    test_metrics = Column(JSON, nullable=True)

    def initialize_from_config(self, run_config):
        self.learning_rate = run_config['learning_rate']
        self.activation = run_config['activation']
        self.optimizer = run_config['optimizer_name']
        self.layers = json.dumps(run_config['layers'])
        self.batch_size = run_config['batch_size']
        self.epochs = run_config['epochs']

    def complete(self, job, update: CompleteRunRequest, db: Session):
        """
        Mark the run as complete.
        """
        from axio_common.models import Device
        self.completed_at = current_time()
        self.updated_at = current_time()
        self.duration = (self.completed_at - self.created_at).total_seconds()
        self.epochs_completed = update.epochs_completed
        self.is_current = False

        # Save the metrics
        self.train_metrics = update.train_metrics
        self.val_metrics = update.val_metrics
        self.test_metrics = update.test_metrics

        # Get best test metrics from the best run of the current job
        best_run = db.query(Run).filter(Run.job_id == self.job_id, Run.is_best).first()
        if best_run is None or update.test_metrics["TE-all"]["mae"][-1] < best_run.test_metrics["TE-all"]["mae"][-1]:
            self.is_best = True
            if best_run is not None:
                logger.info(f"Run {self.number} is the new best run for job {self.job_id} "
                            f"({update.test_metrics['TE-all']['mae'][-1]:.4f} < "
                            f"{best_run.test_metrics['TE-all']['mae'][-1]:.4f})")
                best_run.is_best = False
            else:
                logger.info(f"Run {self.number} is the first best run for job {self.job_id} "
                            f"({update.test_metrics['TE-all']['mae'][-1]:.4f})")

            # Get the associated device and attach the best run metrics
            if job:
                device = db.query(Device).filter(Device.axf_id == job.device_axf_id).first()
                if device:
                    device.update_best_metrics(update, self.number, job)
                    db.add(device)
        else:
            logger.info(f"Run {self.number} is not the best run for job {self.job_id} "
                        f"({update.test_metrics['TE-all']['mae'][-1]:.4f} > "
                        f"{best_run.test_metrics['TE-all']['mae'][-1]:.4f})")
        db.add(self)

    def to_dict(self):
        return {
            "id": self.id,
            "job_id": self.job_id,
            "number": self.number,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "duration": self.duration,
            "completed_at": self.completed_at,
            "learning_rate": self.learning_rate,
            "activation": self.activation,
            "optimizer": self.optimizer,
            "layers": self.layers,
            "batch_size": self.batch_size,
            "epochs": self.epochs,
            "epochs_completed": self.epochs_completed,
            "is_best": self.is_best,
            "is_current": self.is_current,
            "train_metrics": self.train_metrics,
            "val_metrics": self.val_metrics,
            "test_metrics": self.test_metrics
        }

    @classmethod
    def from_dict(cls, data):
        """
        Create a Run object from a dictionary.
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
        Update the Run object from a dictionary.
        Ignores None values unless explicitly updating a field to None.
        """
        for key, value in data.items():
            if key in self.to_dict() and value is not None:  # Only update if not None
                setattr(self, key, value)
