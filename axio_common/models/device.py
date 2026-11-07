from datetime import datetime
from typing import Optional
from pydantic import BaseModel
from sqlalchemy import Column, Index, String, TEXT, DateTime, Integer, JSON, BigInteger
from sqlalchemy.orm import relationship

from axio_common.database import Base
from axio_common.logger import logger
from axio_common.utils.model_utils import current_time
from axio_common.models.loadcell import LOADCELL_TYPE_NAMES

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
    "14": "Load Cells v1.2",
    "15": "Force Pedal - Left",
    "16": "Force Pedal - Right",
    # Same board as "14" Load Cell v1.2 (ATI Axia90-F-1000 six-axis cell);
    # name matches AxioCanon deviceTypes/deviceType/17.
    "17": "Axia90-F-1000",
    # Dev kit (axioforce-devkit-firmware): STM32N657 base + flex with 5x
    # HAL3304 magnetometers, ISM330BX IMU and a TMP118.
    "18": "Dev Kit",
    "af": "Axiocell",
}
# Six-axis load cell (axioforce-loadcell-firmware, ring0 board): 19, 1a..1f.
# The names live with the rest of the load cell rules in `loadcell.py`.
TYPE_ID_NAME_MAP.update(LOADCELL_TYPE_NAMES)

# The device's model heads and the axis of TE-all MAE that ranks each:
# z for force, x for moment. Shared by update/recompute/pin.
MODEL_HEADS = ("force", "moment")
HEAD_AXIS = {"force": -1, "moment": 0}


class BestPinRefused(ValueError):
    """`Device.pin_best` / `unpin_best` refused the request (HTTP 400)."""


class BestPinNotFound(LookupError):
    """`Device.pin_best` found no such job or run on the device (HTTP 404)."""


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
    size: Optional[str] = None
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
    # Operator pin on a head (see Device.pin_best). NULL pinned_at = the head
    # follows the automatic lowest-MAE rule.
    best_force_pinned_at: Optional[datetime] = None
    best_force_pinned_by: Optional[str] = None
    best_force_pin_note: Optional[str] = None
    best_moment_pinned_at: Optional[datetime] = None
    best_moment_pinned_by: Optional[str] = None
    best_moment_pin_note: Optional[str] = None
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
    # Physical size of the unit. Insole-only in practice (EU shoe size, e.g.
    # "41"); NULL for force plates and load cells. String, not Integer, to match
    # CalibrationBucketSession.size — that field already allows "10.5" and
    # "M9/W10.5", and a second, narrower convention for the same quantity would
    # only invite conversion bugs. Sourced from the DAQ at calibration-session
    # start (size is required there for insoles) and backfilled for units that
    # predate that field.
    #
    # NOTE: a 09 (left) and 0a (right) sharing an ID suffix are NOT a pair —
    # left and right are manufactured and numbered independently, in build
    # order. Differing sizes at the same suffix are expected, not a data error.
    size = Column(String, nullable=True)
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

    # Operator pin, one per head (#1305). The best_* pointer above is the
    # device's SHIPPED model. By default it follows the lowest TE-all MAE, but
    # a model can be better in ways that MAE at the calibration temperature
    # does not measure (a thermal-normalized retrain, #1299), so an admin can
    # pin a head to a chosen run. `pinned_at` NOT NULL means pinned: neither
    # `update_best_metrics` nor `recompute_best_metrics` moves that head, except
    # that recompute drops a pin whose job was deleted, so a deleted model is
    # never shipped. Written only by `pin_best` / `unpin_best` (and cleared by
    # that recompute).
    best_force_pinned_at = Column(DateTime(timezone=True), nullable=True)
    best_force_pinned_by = Column(String, nullable=True)
    best_force_pin_note = Column(TEXT, nullable=True)
    best_moment_pinned_at = Column(DateTime(timezone=True), nullable=True)
    best_moment_pinned_by = Column(String, nullable=True)
    best_moment_pin_note = Column(TEXT, nullable=True)

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

        if self.is_pinned(job.model_type):
            logger.info(
                f"Device {self.axf_id} best {job.model_type} is pinned to "
                f"{getattr(self, f'best_{job.model_type}_timestamp')}/"
                f"{getattr(self, f'best_{job.model_type}_run')} by "
                f"{getattr(self, f'best_{job.model_type}_pinned_by')}; run "
                f"{job.timestamp}/{run_number} left it untouched")
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

    def recompute_best_metrics(self, db, model_types=MODEL_HEADS):
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

        A pinned head (`pin_best`) is left untouched while its run survives. If
        the pinned run's job was deleted (soft or hard) or the run is gone, the
        pin is dropped and the head is recomputed, so a deleted model is never
        shipped. `model_types` limits the pass to some heads (`unpin_best`).
        """
        from axio_common.models import Run, Job

        activity, metric = "TE-all", "mae"
        for model_type in model_types:
            axis = HEAD_AXIS[model_type]
            if self.is_pinned(model_type):
                if self._pinned_run(db, model_type) is not None:
                    logger.info(
                        f"Device {self.axf_id} best {model_type} is pinned to "
                        f"{getattr(self, f'best_{model_type}_timestamp')}/"
                        f"{getattr(self, f'best_{model_type}_run')} by "
                        f"{getattr(self, f'best_{model_type}_pinned_by')}; "
                        f"recompute left it untouched.")
                    continue
                logger.warning(
                    f"Device {self.axf_id} best {model_type} was pinned to "
                    f"{getattr(self, f'best_{model_type}_timestamp')}/"
                    f"{getattr(self, f'best_{model_type}_run')}, whose job is "
                    f"deleted or gone; pin dropped, recomputing.")
                self._clear_pin(model_type)

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
                self._set_head(model_type, None)
                logger.info(f"Device {self.axf_id} best {model_type} metrics cleared "
                            f"(no surviving run with {activity} {metric}).")
                continue

            self._set_head(model_type, best_run)
            logger.info(f"Device {self.axf_id} best {model_type} metrics recomputed: "
                        f"{best_run.job.timestamp}/{best_run.number} ({best_value:.4f}).")

    # ---- operator pin ------------------------------------------------------

    def is_pinned(self, model_type) -> bool:
        """True when an operator has pinned this head (`pin_best`)."""
        if model_type not in HEAD_AXIS:
            return False
        return getattr(self, f"best_{model_type}_pinned_at", None) is not None

    def _set_head(self, model_type, run):
        """Write the five pointer fields of a head from `run` (None clears).

        The one place both `recompute_best_metrics` and `pin_best` write a
        head, so a pinned head is set exactly as recompute would set it.
        """
        setattr(self, f"best_{model_type}_run", run.number if run else None)
        setattr(self, f"best_{model_type}_timestamp", run.job.timestamp if run else None)
        setattr(self, f"best_{model_type}_train_metrics", run.train_metrics if run else None)
        setattr(self, f"best_{model_type}_val_metrics", run.val_metrics if run else None)
        setattr(self, f"best_{model_type}_test_metrics", run.test_metrics if run else None)

    def _clear_pin(self, model_type):
        setattr(self, f"best_{model_type}_pinned_at", None)
        setattr(self, f"best_{model_type}_pinned_by", None)
        setattr(self, f"best_{model_type}_pin_note", None)

    def _pinned_run(self, db, model_type):
        """The run a pinned head points at, or None when its job is deleted or gone."""
        from axio_common.models import Run, Job

        ts = getattr(self, f"best_{model_type}_timestamp")
        number = getattr(self, f"best_{model_type}_run")
        if ts is None or number is None:
            return None
        return (
            db.query(Run)
            .join(Job, Run.job_id == Job.id)
            .filter(
                Job.device_axf_id == self.axf_id,
                Job.model_type == model_type,
                Job.timestamp == ts,
                Job.status != "deleted",
                Run.number == number,
            )
            .first()
        )

    def pin_best(self, db, model_type, job_timestamp, run_number, actor, note=None):
        """Pin a head of this device to a hand-picked run (#1305).

        The run becomes the head's shipped model and stays it: completions and
        recomputes leave a pinned head alone, until `unpin_best` or the job's
        deletion. Validates first and writes nothing on refusal:
          - `model_type` is 'force' or 'moment';
          - the job belongs to THIS device, is of that model_type, has that
            integer timestamp (the job UID), is status 'completed';
          - the run exists in that job and has TE-all MAE metrics.
        Raises `BestPinNotFound` (no such job/run) or `BestPinRefused`
        (anything else). Re-pinning a pinned head moves the pin. Does NOT
        commit; the caller owns the transaction.
        """
        from axio_common.models import Run, Job

        if model_type not in HEAD_AXIS:
            raise BestPinRefused(f"model_type must be one of {list(MODEL_HEADS)}, not {model_type!r}")
        actor = (actor or "").strip()
        if not actor:
            raise BestPinRefused("a pin needs an actor (who pinned it)")
        try:
            job_timestamp, run_number = int(job_timestamp), int(run_number)
        except (TypeError, ValueError):
            raise BestPinRefused("job_timestamp and run_number must be integers")

        jobs = (
            db.query(Job)
            .filter(Job.device_axf_id == self.axf_id, Job.timestamp == job_timestamp)
            .all()
        )
        if not jobs:
            raise BestPinNotFound(f"device {self.axf_id} has no job {job_timestamp}")
        typed = [j for j in jobs if j.model_type == model_type]
        if not typed:
            kinds = "/".join(sorted({str(j.model_type) for j in jobs}))
            raise BestPinRefused(
                f"job {job_timestamp} of {self.axf_id} is a {kinds} job, not {model_type}")
        live = [j for j in typed if j.status != "deleted"]
        if not live:
            raise BestPinRefused(f"job {job_timestamp} of {self.axf_id} is deleted")
        done = [j for j in live if j.status == "completed"]
        if not done:
            raise BestPinRefused(
                f"job {job_timestamp} of {self.axf_id} is {live[0].status}, not completed")

        run = (
            db.query(Run)
            .filter(Run.job_id.in_([j.id for j in done]), Run.number == run_number)
            .first()
        )
        if run is None:
            raise BestPinNotFound(f"job {job_timestamp} of {self.axf_id} has no run {run_number}")
        block = (run.test_metrics or {}).get("TE-all") or {}
        try:
            value = float(block["mae"][HEAD_AXIS[model_type]])
        except (KeyError, IndexError, TypeError, ValueError):
            raise BestPinRefused(
                f"run {job_timestamp}/{run_number} has no TE-all MAE metrics")

        previous = (getattr(self, f"best_{model_type}_timestamp"),
                    getattr(self, f"best_{model_type}_run"))
        self._set_head(model_type, run)
        setattr(self, f"best_{model_type}_pinned_at", current_time())
        setattr(self, f"best_{model_type}_pinned_by", actor)
        setattr(self, f"best_{model_type}_pin_note", (note or "").strip() or None)
        logger.info(
            f"Device {self.axf_id} best {model_type} PINNED to {job_timestamp}/{run_number} "
            f"(TE-all mae {value:.4f}) by {actor}; was {previous[0]}/{previous[1]}")
        return run

    def unpin_best(self, db, model_type):
        """Drop the pin on a head and recompute it by the automatic rule.

        Idempotent: unpinning an unpinned head still recomputes it. Does NOT
        commit.
        """
        if model_type not in HEAD_AXIS:
            raise BestPinRefused(f"model_type must be one of {list(MODEL_HEADS)}, not {model_type!r}")
        was = getattr(self, f"best_{model_type}_pinned_by")
        self._clear_pin(model_type)
        logger.info(f"Device {self.axf_id} best {model_type} unpinned "
                    f"(was pinned by {was}); recomputing")
        self.recompute_best_metrics(db, model_types=(model_type,))

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
            "size": self.size,
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
            "best_force_pinned_at": self.best_force_pinned_at,
            "best_force_pinned_by": self.best_force_pinned_by,
            "best_force_pin_note": self.best_force_pin_note,
            "best_moment_pinned_at": self.best_moment_pinned_at,
            "best_moment_pinned_by": self.best_moment_pinned_by,
            "best_moment_pin_note": self.best_moment_pin_note,
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
                "test_metrics": self.best_force_test_metrics,
                **self._pin_dict("force"),
            },
            "moment": {
                "run": self.best_moment_run,
                "timestamp": self.best_moment_timestamp,
                "train_metrics": self.best_moment_train_metrics,
                "val_metrics": self.best_moment_val_metrics,
                "test_metrics": self.best_moment_test_metrics,
                **self._pin_dict("moment"),
            }
        }

    def _pin_dict(self, model_type):
        """The pin of a head, for `best_metrics()` (pinned False = automatic)."""
        return {
            "pinned": self.is_pinned(model_type),
            "pinned_at": getattr(self, f"best_{model_type}_pinned_at"),
            "pinned_by": getattr(self, f"best_{model_type}_pinned_by"),
            "pin_note": getattr(self, f"best_{model_type}_pin_note"),
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

