"""The load cell's calibration captures, indexed, and the job made from them (#1408).

**The index.** axio-server's bucket sync walks `loadcell/<serial>/<segment>/`
(axio-common `storage/loadcell.py`) into two tables, the load cell's
counterpart of `calibration_bucket_sessions` / `calibration_bucket_files`:

  - `LoadCellCaptureSession` (`loadcell_capture_sessions`): one row per
    (serial, date segment). Keyed by the **serial**, never a device id: the
    captures are taken before the unit has a type, and the bucket session
    table needs a `devices` row (and a type) it cannot have. `is_test` marks a
    `test-<iso-date>` upload (anything simulated), which is listed and is
    never calibration data. `reported_serial` / `reported_mcu_uid` /
    `reported_type` are what the session's tests.txt says, so a capture filed
    under the wrong serial is caught before a job trains on it.
  - `LoadCellCaptureFile` (`loadcell_capture_files`): one row per object,
    with its part (`train` | `test` | `tests_txt` | `sidecar` | `other`), the
    joined activity (`TR-GRID`) of a capture, size and time. `is_hidden` is
    the soft-delete a job honors.

Both are a UX index rebuilt from the bucket, which stays the source of truth.
`serial` is not a ForeignKey to `loadcell_units`: a capture can reach the
bucket before its board is registered.

**The job.** `loadcell_job_config()` is the one place a load cell training
job's config is built, so the server and any script submit the same shape.
The Neuralizer (W6, #1420) is built against exactly three keys of it:

    LOADCELL_RECIPE   the recipe name ("joint_v1")
    LOADCELL_SERIAL   8 lowercase hex, the unit's serial
    LOADCELL_TYPE     "1a".."1f", or "19" for a custom cell

Everything else is the ordinary job shape (TRAIN_INPUT_DIR / TEST_INPUT_DIR
as cache-relative paths, RESULTS_DIR under `loadcell/<serial>/models/`, the
required grid keys a recipe ignores), as a dev kit `DEVKIT_RECIPE` job has.
"""
import re
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence

from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON,
    String, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from axio_common.database import Base
from axio_common.models.loadcell import (
    LOADCELL_TYPE_IDS, format_serial, format_type_id,
)
from axio_common.utils.model_utils import current_time

#: The custom-cell type: a job for it must say so (`custom=True`).
LOADCELL_CUSTOM_TYPE = "19"

#: The three keys the Neuralizer reads to run a load cell recipe. Each must be
#: in its `_JOB_AUTHORITATIVE_KEYS` (default None), so a later job on the same
#: worker never inherits them from the merged config.json.
LOADCELL_JOB_KEYS = ("LOADCELL_RECIPE", "LOADCELL_SERIAL", "LOADCELL_TYPE")

#: A recipe name: lowercase, digits, underscores ("joint_v1"). The server does
#: not keep the Neuralizer's recipe list; an unknown recipe fails the job there.
_RECIPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: The ordinary keys a recipe job still has to carry (the Neuralizer's
#: `_JOB_REQUIRED_KEYS`). A recipe ignores the grid; one predetermined run.
_RECIPE_JOB_DEFAULTS = {
    "OUTPUT_TYPE": "force vector",
    "TRAIN_SAMPLING_RATIO": 1.0,
    "TEST_SAMPLING_RATIO": 1.0,
    "TEST_SIZE": 0.2,
    "RANDOM_STATE": 42,
    "LEARNING_RATES": [0.001],
    "HIDDEN_LAYERS": [[64, 64]],
    "ACTIVATIONS": ["elu"],
    "OPTIMIZERS": ["adam"],
    "EPOCHS": [300],
    "BATCH_SIZES": [256],
}


def format_recipe(name) -> str:
    text = (name or "").strip() if isinstance(name, str) else ""
    if not _RECIPE_RE.match(text):
        raise ValueError(
            f"recipe {name!r} is not a recipe name (lowercase letters, digits "
            f"and underscores, e.g. 'joint_v1')")
    return text


def loadcell_job_type(type_id, *, custom: bool = False) -> str:
    """The type a load cell job trains for: `1a`..`1f`, or `19` only when the
    caller says the cell is custom. Refuses the uncalibrated sentinel 0 and
    every other type (ValueError)."""
    text = format_type_id(type_id)          # refuses 0 and non-load-cell types
    if text == LOADCELL_CUSTOM_TYPE and not custom:
        raise ValueError(
            "type 19 is the custom load cell; a job for it must be marked "
            "custom")
    return text


def _compound_name(iso_dates: Iterable[str]) -> str:
    """The results folder name from session dates, as the Submit page and the
    Neuralizer's GUI build it ("12.20.31.2024")."""
    parsed = sorted(tuple(int(x) for x in d.split("-")) for d in iso_dates)
    if not parsed:
        return "UnknownDate"
    merged: List[str] = []
    year, prev_month = parsed[0][0], 0
    for y, m, d in parsed:
        if y != year:
            merged[-1] += f".{year}"
            year = y
        merged.append(f"{m:02d}.{d:02d}" if m != prev_month else f"{d:02d}")
        prev_month = m
    merged[-1] += f".{year:04d}"
    return ".".join(merged)


def loadcell_job_config(serial, type_id, recipe, *, train_keys: Sequence[str],
                        test_keys: Sequence[str], custom: bool = False,
                        notes: str = "") -> Dict[str, object]:
    """The job config of a load cell training job, from bucket keys.

    Refuses (ValueError) a bad serial, type or recipe, and any key that is not
    one of this unit's real captures (a `test-<iso>` upload, another serial,
    a sidecar: `storage.loadcell.loadcell_capture_keys_for_job`), and a job
    with no train or no test capture.
    """
    from axio_common.storage.loadcell import (
        PART_TEST, PART_TRAIN, loadcell_cache_relative,
        loadcell_capture_keys_for_job, loadcell_models_prefix,
        parse_loadcell_key,
    )
    serial = format_serial(serial)
    type_text = loadcell_job_type(type_id, custom=custom)
    recipe = format_recipe(recipe)
    train = loadcell_capture_keys_for_job(serial, train_keys)
    test = loadcell_capture_keys_for_job(serial, test_keys)
    if train[PART_TEST] or test[PART_TRAIN]:
        raise ValueError("a train/ capture was listed as test data, or the "
                         "reverse")
    train_list, test_list = train[PART_TRAIN], test[PART_TEST]
    if not train_list:
        raise ValueError(f"load cell {serial}: no train capture selected")
    if not test_list:
        raise ValueError(f"load cell {serial}: no test capture selected; the "
                         f"held-out figures need one")
    keys = list(train_list) + list(test_list)
    sessions = sorted({parse_loadcell_key(k).segment for k in keys})
    results = (f"{loadcell_models_prefix(serial)}"
               f"{_compound_name(sessions)}")
    slug = _RECIPE_JOB_DEFAULTS["OUTPUT_TYPE"].replace(" ", "_")
    config: Dict[str, object] = {
        "DEVICE_ID": f"{type_text}-{serial}",
        "DEVICE_NAME": None,
        "TRAIN_INPUT_DIR": [loadcell_cache_relative(k) for k in train_list],
        "TEST_INPUT_DIR": [loadcell_cache_relative(k) for k in test_list],
        "SELECTED_DIRS": [loadcell_cache_relative(
            f"loadcell/{serial}/{s}").rstrip("/") for s in sessions],
        "RESULTS_DIR": results,
        "NN_RESULTS_DIR": f"{results}/nn_results/{slug}",
        "BEST_MODEL_DIR": f"{results}/best_model/{slug}",
        **{k: (list(v) if isinstance(v, list) else v)
           for k, v in _RECIPE_JOB_DEFAULTS.items()},
        "NOTES": notes or "",
        "LOADCELL_RECIPE": recipe,
        "LOADCELL_SERIAL": serial,
        "LOADCELL_TYPE": type_text,
    }
    return config


def loadcell_job_fields(config) -> Optional[Dict[str, str]]:
    """`{"recipe", "serial", "type"}` from a job config (dict or JSON text) of
    a load cell job, or None for any other job."""
    import json
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            return None
    if not isinstance(config, dict) or not config.get("LOADCELL_SERIAL"):
        return None
    return {"recipe": config.get("LOADCELL_RECIPE"),
            "serial": config.get("LOADCELL_SERIAL"),
            "type": config.get("LOADCELL_TYPE")}


# --- the index ---------------------------------------------------------------

class LoadCellCaptureSession(Base):
    __tablename__ = "loadcell_capture_sessions"
    __table_args__ = (
        UniqueConstraint("serial", "date_segment",
                         name="uq_loadcell_capture_session_serial_segment"),
        Index("ix_loadcell_capture_sessions_serial_test", "serial", "is_test"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: 8 lowercase hex; not a FK (a capture can precede registration).
    serial = Column(String, nullable=False, index=True)
    #: The bucket's date segment: `2026-11-10` or `test-2026-11-09`.
    date_segment = Column(String, nullable=False)
    #: The date in it, ISO.
    date_iso = Column(String, nullable=False, index=True)
    #: A `test-<iso>` upload: listed, never calibration data.
    is_test = Column(Boolean, nullable=False, default=False)
    bucket_prefix = Column(String, nullable=False)

    n_train = Column(Integer, nullable=False, default=0)
    n_test = Column(Integer, nullable=False, default=0)
    n_sidecars = Column(Integer, nullable=False, default=0)
    total_files = Column(Integer, nullable=False, default=0)
    total_bytes = Column(BigInteger, nullable=False, default=0)
    last_upload_at = Column(DateTime(timezone=True), nullable=True)

    tests_txt_uploaded_at = Column(DateTime(timezone=True), nullable=True)
    #: tests.txt header, `parse_loadcell_tests_txt()["header"]`.
    tests_txt_fields = Column(JSON, nullable=True)
    #: Joined activity ids the tests.txt lists ("TR-GRID", ...).
    expected_activities = Column(JSON, nullable=True)
    calibrator_name = Column(String, nullable=True)
    #: What the tests.txt says about the board (None when it says nothing).
    reported_serial = Column(String, nullable=True)
    reported_mcu_uid = Column(String, nullable=True)
    reported_type = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=current_time)
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=current_time, onupdate=current_time)

    files = relationship("LoadCellCaptureFile", back_populates="session",
                         cascade="all, delete-orphan", lazy="select",
                         order_by="LoadCellCaptureFile.bucket_key")


class LoadCellCaptureFile(Base):
    __tablename__ = "loadcell_capture_files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("loadcell_capture_sessions.id",
                                            ondelete="CASCADE"),
                        nullable=False, index=True)
    bucket_key = Column(String, nullable=False, unique=True)
    filename = Column(String, nullable=False)
    #: `train` | `test` | `tests_txt` | `sidecar` | `other`.
    part = Column(String, nullable=False)
    #: Joined activity of a capture ("TR-GRID"); None otherwise.
    activity_id = Column(String, nullable=True)
    size = Column(BigInteger, nullable=False, default=0)
    last_modified_at = Column(DateTime(timezone=True), nullable=True)
    #: Soft delete: a hidden capture is never put in a job.
    is_hidden = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=current_time)
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=current_time, onupdate=current_time)

    session = relationship("LoadCellCaptureSession", back_populates="files")


# --- API shapes --------------------------------------------------------------

class LoadCellCaptureFileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    bucket_key: str
    filename: str
    part: str
    activity_id: Optional[str] = None
    size: int
    last_modified_at: Optional[datetime] = None
    is_hidden: bool = False


class LoadCellCaptureSessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    serial: str
    date_segment: str
    date_iso: str
    is_test: bool
    bucket_prefix: str
    n_train: int
    n_test: int
    n_sidecars: int
    total_files: int
    total_bytes: int
    last_upload_at: Optional[datetime] = None
    tests_txt_uploaded_at: Optional[datetime] = None
    tests_txt_fields: Optional[dict] = None
    expected_activities: Optional[List[str]] = None
    calibrator_name: Optional[str] = None
    reported_serial: Optional[str] = None
    reported_mcu_uid: Optional[str] = None
    reported_type: Optional[str] = None
    files: Optional[List[LoadCellCaptureFileResponse]] = None


class LoadCellTrainingJobRequest(BaseModel):
    """Body of `POST /loadcell/units/{serial}/training-jobs`."""
    #: Date segments of the sessions to train on (ISO dates; never `test-`).
    sessions: List[str]
    #: `1a`..`1f`; `19` only with `custom`. Never 0.
    device_type_id: str
    recipe: str
    custom: bool = False
    #: Capture keys of those sessions to leave out.
    exclude_keys: Optional[List[str]] = None
    notes: Optional[str] = None
    #: Restrict to these daemons (None = any daemon).
    allowed_hostnames: Optional[List[str]] = None
    priority: Optional[int] = None


__all__ = [
    "LOADCELL_CUSTOM_TYPE", "LOADCELL_JOB_KEYS", "LOADCELL_TYPE_IDS",
    "format_recipe", "loadcell_job_type", "loadcell_job_config",
    "loadcell_job_fields",
    "LoadCellCaptureSession", "LoadCellCaptureFile",
    "LoadCellCaptureSessionResponse", "LoadCellCaptureFileResponse",
    "LoadCellTrainingJobRequest",
]
