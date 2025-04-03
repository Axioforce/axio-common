from datetime import datetime
from typing import Optional
from pydantic import BaseModel
from sqlalchemy import Column, String, TEXT, DateTime, Integer, JSON, BigInteger

from axio_common.database import Base
from axio_common.logger import logger
from axio_common.utils.model_utils import current_time

TYPE_ID_NAME_MAP = {
    "01": "(Deprecated) Small Force Plate",
    "02": "(Deprecated) Large Force Plate",
    "03": "(Deprecated) Shoe Insole",
    "04": "Demo Kit",
    "05": "Load Cells",
    "06": "Launch Pad Lite",
    "07": "Launch Pad",
    "08": "Launch Pad XL",
    "09": "Shoe Insole Left",
    "0a": "Shoe Insole Right",
    "0b": "Rowing Ergometer Left",
    "0c": "Rowing Ergometer Right",
    "0d": "Rat Pad Right",
    "0e": "Rat Pad Left",
}


class DeviceResponse(BaseModel):
    axf_id: str
    name: str = None
    type_id: str = None
    type_name: str = None
    device_metadata: Optional[dict] = None  # Additional data about the device
    created_at: datetime = current_time()
    updated_at: datetime = current_time()
    best_force_run: Optional[int] = None
    best_force_timestamp: Optional[int] = None
    best_force_train_metrics: Optional[dict] = None
    best_force_val_metrics: Optional[dict] = None
    best_force_test_metrics: Optional[dict] = None
    best_moment_run: Optional[int] = None
    best_moment_timestamp: Optional[int] = None
    best_moment_train_metrics: Optional[dict] = None
    best_moment_val_metrics: Optional[dict] = None
    best_moment_test_metrics: Optional[dict] = None

    class Config:
        from_attributes = True  # Enables SQLAlchemy model compatibility


class Device(Base):
    __tablename__ = "devices"
    axf_id = Column(String, primary_key=True, unique=True)
    name = Column(String)
    type_id = Column(String, nullable=False)
    type_name = Column(String, nullable=False)
    device_metadata = Column(TEXT, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=current_time, onupdate=current_time)
    best_force_run = Column(Integer, nullable=True)
    best_force_timestamp = Column(BigInteger, nullable=True)
    best_force_train_metrics = Column(JSON, nullable=True)
    best_force_val_metrics = Column(JSON, nullable=True)
    best_force_test_metrics = Column(JSON, nullable=True)
    best_moment_run = Column(Integer, nullable=True)
    best_moment_timestamp = Column(BigInteger, nullable=True)
    best_moment_train_metrics = Column(JSON, nullable=True)
    best_moment_val_metrics = Column(JSON, nullable=True)
    best_moment_test_metrics = Column(JSON, nullable=True)

    def __init__(self, axf_id: str):
        """
        Initialize a new device object.
        """
        super().__init__(axf_id=axf_id)
        self.type_id = axf_id.split(".")[0]
        self.name = TYPE_ID_NAME_MAP.get(self.type_id, "Unknown Device")
        self.type_name = TYPE_ID_NAME_MAP.get(self.type_id, "Unknown Device")
        logger.info(f"Device {axf_id} created, type: {self.type_name}")

    def update_best_metrics(self, update, run_number, job):
        """
        Update the best metrics for the device.
        """
        train_metrics = update.train_metrics
        val_metrics = update.val_metrics
        test_metrics = update.test_metrics

        activity = "TE-all"
        metric = "mae"

        if job.model_type == "force":
            best_run_attr = "best_force_run"
            best_metrics_attr = "best_force_test_metrics"
            axis = -1  # Use the z axis
        elif job.model_type == "moment":
            best_run_attr = "best_moment_run"
            best_metrics_attr = "best_moment_test_metrics"
            axis = 0  # Use the x axis
        else:
            logger.warning(f"Unknown model type '{job.model_type}' for device {self.axf_id}")
            return

        best_run = getattr(self, best_run_attr, None)
        best_metrics = getattr(self, best_metrics_attr, {})

        if best_metrics is None:
            best_metrics = {}

        # Handle missing activity key
        if activity not in best_metrics:
            if "all" in best_metrics:
                best_metrics[activity] = best_metrics["all"]
            else:
                best_run = None  # Reset best run if no metrics exist

        # Determine if the new test metric is better
        new_metric_value = test_metrics[activity][metric][axis]
        best_metric_value = best_metrics[activity][metric][axis] if best_run else float("inf")

        if best_run is None or new_metric_value < best_metric_value:
            if best_run is not None:
                logger.info(
                    f"Device {self.axf_id} best {job.model_type} metrics updated: {job.timestamp}/{run_number} "
                    f"({new_metric_value:.4f} < {best_metric_value:.4f})"
                )
            else:
                logger.info(
                    f"Device {self.axf_id} best {job.model_type} metrics updated: {job.timestamp}/{run_number} "
                    f"({new_metric_value:.4f})"
                )

            setattr(self, best_run_attr, run_number)
            setattr(self, best_metrics_attr, test_metrics)
            setattr(self, f"best_{job.model_type}_timestamp", job.timestamp)
            setattr(self, f"best_{job.model_type}_train_metrics", train_metrics)
            setattr(self, f"best_{job.model_type}_val_metrics", val_metrics)
        else:
            logger.info(f"Run {run_number} did not improve {job.model_type} metrics for device {self.axf_id}")

    def to_dict(self):
        """
        Convert the device object to a dictionary for Firebase.
        """
        return {
            "axf_id": self.axf_id,
            "name": self.name,
            "type_id": self.type_id,
            "type_name": self.type_name if self.type_name else self.name,
            "device_metadata": self.device_metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "best_force_run": self.best_force_run,
            "best_force_timestamp": self.best_force_timestamp,
            "best_force_train_metrics": self.best_force_train_metrics,
            "best_force_val_metrics": self.best_force_val_metrics,
            "best_force_test_metrics": self.best_force_test_metrics,
            "best_moment_run": self.best_moment_run,
            "best_moment_timestamp": self.best_moment_timestamp,
            "best_moment_train_metrics": self.best_moment_train_metrics,
            "best_moment_val_metrics": self.best_moment_val_metrics,
            "best_moment_test_metrics": self.best_moment_test_metrics
        }

    @classmethod
    def from_dict(cls, data):
        """
        Create a Device object from a dictionary.
        Ensures required fields are present and assigns sensible defaults.
        """
        valid_keys = {column.name for column in cls.__table__.columns}
        filtered_data = {key: value for key, value in data.items() if key in valid_keys}

        # Extract axf_id separately, since __init__ only accepts that
        axf_id = filtered_data.pop("axf_id", None)
        if axf_id is None:
            raise ValueError("Device must have an axf_id")

        # Create the instance with only axf_id
        device = cls(axf_id=axf_id)

        # Assign remaining attributes dynamically
        for key, value in filtered_data.items():
            setattr(device, key, value)

        return device

    def best_metrics(self):
        """
        Return the best metrics for the device.
        """
        return {
            "axf_id": self.axf_id,
            "force": {
                "run": self.best_force_run,
                "timestamp": self.best_force_timestamp,
                "train_metrics": self.best_force_train_metrics,
                "val_metrics": self.best_force_val_metrics,
                "test_metrics": self.best_force_test_metrics
            },
            "moment": {
                "run": self.best_moment_run,
                "timestamp": self.best_moment_timestamp,
                "train_metrics": self.best_moment_train_metrics,
                "val_metrics": self.best_moment_val_metrics,
                "test_metrics": self.best_moment_test_metrics
            }
        }

    def update_from_server(self, device):
        """
        Update the device object from a server response.
        """
        for key, value in device.items():
            if key in self.to_dict() and value is not None:
                setattr(self, key, value)

class BestDeviceMetricsResponse(BaseModel):
    axf_id: str
    force: dict
    moment: dict

