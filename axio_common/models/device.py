from datetime import datetime
from typing import Optional
from pydantic import BaseModel
from sqlalchemy import Column, Index, String, TEXT, DateTime, Integer, JSON, BigInteger
from sqlalchemy.orm import relationship

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
    "0f": "Hawkin Dynamic Prototype",
    "10": "Launch Pad Lite v1.2",
    "11": "Launch Pad v1.2",
    "12": "Launch Pad XL v1.2",
    "13": "Robotic Skin",
    "af": "Axiocell",
}


class DeviceResponse(BaseModel):
    axf_id: str
    name: str = None
    type_id: str = None
    type_name: str = None
    device_metadata: Optional[dict] = None  # Additional data about the device
    created_at: datetime = current_time()
    updated_at: datetime = current_time()
    # Date/time the device was physically assembled. Set from the local
    # AxioforceDynamoPy "Initialize" click; nullable for devices that pre-date
    # this field. assembled_date_history is an append-only audit list of
    # {"value": iso_str, "previous": iso_str|None, "changed_at": iso_str,
    #  "reason": str|None} entries — every edit appends one row.
    assembled_date: Optional[datetime] = None
    assembled_date_history: Optional[list] = None
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
    anomaly_critical: Optional[int] = 0
    anomaly_warning: Optional[int] = 0
    anomaly_details: Optional[list] = None

    class Config:
        from_attributes = True  # Enables SQLAlchemy model compatibility


class Device(Base):
    __tablename__ = "devices"

    axf_id = Column(String, primary_key=True, unique=True)
    name = Column(String)
    type_id = Column(String, nullable=False, index=True)
    type_name = Column(String, nullable=False)

    # Relationships
    jobs = relationship("Job", back_populates="device", lazy="select")
    device_metadata = Column(TEXT, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=current_time, onupdate=current_time)
    # Physical assembly timestamp — sourced from the local "Initialize" click in
    # AxioforceDynamoPy. Nullable so existing rows don't need backfilling. Edits
    # are tracked in assembled_date_history (JSON list, append-only).
    assembled_date = Column(DateTime(timezone=True), nullable=True)
    assembled_date_history = Column(JSON, nullable=True)
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

    # Anomaly detection results (persisted at run completion)
    anomaly_critical = Column(Integer, nullable=True, default=0)
    anomaly_warning = Column(Integer, nullable=True, default=0)
    anomaly_details = Column(JSON, nullable=True)

    def __init__(self, axf_id: str, name: Optional[str] = None):
        """
        Initialize a new device object.
        """
        super().__init__(axf_id=axf_id, name=name)
        self.type_id = axf_id.split(".")[0]
        self.name = name or TYPE_ID_NAME_MAP.get(self.type_id, "Unknown Device")
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

    def recompute_best_metrics(self, db):
        """
        Recompute best_force_* / best_moment_* from scratch over every run that
        belongs to a NON-deleted job for this device.

        `update_best_metrics` is a monotonic pointer: it only ever overwrites the
        best when a *better* run completes, and it never re-evaluates when a job
        (and its runs) is deleted. So a deleted job's run can stay frozen as the
        device best forever, and a clean re-train can't dislodge it if the deleted
        run had a lower MAE. This rebuilds the pointer from the surviving runs,
        using the same metric selection as `update_best_metrics` (TE-all mae,
        z-axis for force / x-axis for moment). If no eligible run remains for a
        head, that head's best_* columns are cleared to NULL.
        """
        from axio_common.models import Run, Job

        activity, metric = "TE-all", "mae"
        for model_type, axis in (("force", -1), ("moment", 0)):
            runs = (
                db.query(Run)
                .join(Job, Run.job_id == Job.id)
                .filter(
                    Job.device_axf_id == self.axf_id,
                    Job.model_type == model_type,
                    Job.status != "deleted",
                )
                .all()
            )

            best_value, best_run = float("inf"), None
            for run in runs:
                tm = run.test_metrics or {}
                block = tm.get(activity) or tm.get("all")
                if not block or metric not in block:
                    continue
                try:
                    value = block[metric][axis]
                except (KeyError, IndexError, TypeError):
                    continue
                if value < best_value:
                    best_value, best_run = value, run

            if best_run is None:
                setattr(self, f"best_{model_type}_run", None)
                setattr(self, f"best_{model_type}_timestamp", None)
                setattr(self, f"best_{model_type}_train_metrics", None)
                setattr(self, f"best_{model_type}_val_metrics", None)
                setattr(self, f"best_{model_type}_test_metrics", None)
                logger.info(f"Device {self.axf_id} best {model_type} metrics cleared "
                            f"(no surviving run with {activity} {metric}).")
                continue

            setattr(self, f"best_{model_type}_run", best_run.number)
            setattr(self, f"best_{model_type}_timestamp", best_run.job.timestamp)
            setattr(self, f"best_{model_type}_train_metrics", best_run.train_metrics)
            setattr(self, f"best_{model_type}_val_metrics", best_run.val_metrics)
            setattr(self, f"best_{model_type}_test_metrics", best_run.test_metrics)
            logger.info(f"Device {self.axf_id} best {model_type} metrics recomputed: "
                        f"{best_run.job.timestamp}/{best_run.number} ({best_value:.4f}).")

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
            "assembled_date": self.assembled_date,
            "assembled_date_history": self.assembled_date_history,
            "best_force_run": self.best_force_run,
            "best_force_timestamp": self.best_force_timestamp,
            "best_force_train_metrics": self.best_force_train_metrics,
            "best_force_val_metrics": self.best_force_val_metrics,
            "best_force_test_metrics": self.best_force_test_metrics,
            "best_moment_run": self.best_moment_run,
            "best_moment_timestamp": self.best_moment_timestamp,
            "best_moment_train_metrics": self.best_moment_train_metrics,
            "best_moment_val_metrics": self.best_moment_val_metrics,
            "best_moment_test_metrics": self.best_moment_test_metrics,
            "anomaly_critical": self.anomaly_critical,
            "anomaly_warning": self.anomaly_warning,
            "anomaly_details": self.anomaly_details,
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

