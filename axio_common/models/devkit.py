"""
Dev-kit unit tracking: which physical boards make up a shipped unit.

The dev kit (device type `18`) is **two separable boards** — an STM32N657
base board and a flex carrying five HAL3304 magnetometers, an ISM330BX IMU
and a TMP118. Either can be swapped, and a calibration model is only valid
on the flex it was trained against: the same field pattern means different
forces on different sensors, and a mismatched model produces
plausible-looking wrong numbers that nothing downstream would catch.

So this table records both halves, not just the unit.

Design notes, deliberately parallel to `force_plate.py`:
  - `device_axf_id` is the primary key and is **not** a ForeignKey to
    `devices`, for the same reason ForcePlate's isn't: a unit may be built
    and recorded before it has ever run a training job.
  - The hardware identifiers are a **fingerprint, not the serial**. The
    serial is the administratively-assigned `18.xxxxxxxx` device id; these
    columns say which boards were in the unit when it was provisioned, so a
    later swap is detectable rather than silent.
  - `initialized_at` records first provisioning and is immutable;
    `assembled_at` is editable with an audit row, matching ForcePlate.
  - Board swaps are append-only audit rows. Nothing is soft-deleted here:
    the history of what was in a unit is the point of the table.

Identifier semantics (firmware `docs/PROTOCOL.md` § "Board identity"):
  - `flex_fp`  — CRC-32 over the five magnetometers' per-die traceability
                 blocks. Identifies the FLEX.
  - `tmp_uid`  — TMP118 48-bit NIST-traceable unique id. Also the FLEX, and
                 the stronger guarantee of the two.
  - `mcu_uid`  — STM32 96-bit UID. Identifies the BASE board, so between
                 them a swap of either board is detectable.

`flex_fp = '00000000'` is the firmware's UNIDENTIFIED sentinel, not a real
board — a unit whose fingerprint did not form. It must never be stored as
though it were an identity; `unidentified_flex()` exists to make that check
hard to forget, since `18.00000000` otherwise passes the workspace's
device-id validators and looks legitimate.
"""
import re
from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel
from sqlalchemy import (
    Column, String, Text, DateTime, Integer, ForeignKey, Index,
)
from sqlalchemy.orm import Session, relationship

from axio_common.database import Base
from axio_common.logger import logger
from axio_common.utils.model_utils import current_time


#: The firmware's "fingerprint did not form" sentinel (PROTOCOL.md).
UNIDENTIFIED_FLEX_FP = "00000000"

#: The dev kit's device type. Everything in this module is scoped to it: a
#: force plate has no flex and no fingerprint, so none of this applies.
DEVKIT_TYPE_ID = "18"

#: `18.5fdb3916` and the dashed bucket spelling `18-5fdb3916`.
_DEVICE_ID_RE = re.compile(r"^([0-9a-fA-F]{2})[-.]([0-9a-fA-F]{8})$")


def unidentified_flex(flex_fp: Optional[str]) -> bool:
    """True when a reported fingerprint is the UNIDENTIFIED sentinel.

    Call this before persisting or joining on a fingerprint. A capture or a
    provisioning record carrying this value is unattributable, not attributed
    to a board named zero.
    """
    return not flex_fp or flex_fp.strip().lower() == UNIDENTIFIED_FLEX_FP


def flex_fp_from_device_id(device_axf_id: Optional[str]) -> Optional[str]:
    """The flex fingerprint carried inside a dev kit's device id.

    A dev kit's id IS its fingerprint: the SDK builds it as
    ``"18.%08x" % flex_fp`` (axioforce-devkit-sdk, ``Identity.device_id``),
    so the 8-hex tail is the fingerprint and not merely correlated with it.
    That is what lets a devkits row be written server-side at training time,
    where nothing has read the board.

    Returns None for anything that is not a dev-kit id, and None for the
    UNIDENTIFIED sentinel — `18.00000000` parses cleanly and means "no
    fingerprint formed", which must never be stored as one.
    """
    if not isinstance(device_axf_id, str):
        return None
    m = _DEVICE_ID_RE.match(device_axf_id.strip())
    if not m or m.group(1).lower() != DEVKIT_TYPE_ID:
        return None
    fp = m.group(2).lower()
    return None if unidentified_flex(fp) else fp


def is_devkit_id(device_axf_id: Optional[str]) -> bool:
    """True for a type-18 device id in either separator spelling."""
    if not isinstance(device_axf_id, str):
        return False
    m = _DEVICE_ID_RE.match(device_axf_id.strip())
    return bool(m) and m.group(1).lower() == DEVKIT_TYPE_ID


def model_artifact_name(device_axf_id: str, job_timestamp, model_type: str,
                        run_number) -> Optional[str]:
    """`{axf}-{timestamp}-{run}-{f|m}` — the trained model's own name.

    The workspace convention (see the root CLAUDE.md, `Job.timestamp`): the
    packed `YYYYMMDDHHMMSS` job UID is the `<timestamp>` segment of the model
    filename. Storing this on the devkits row is what lets a support answer
    name the exact artifact rather than a date, which is what someone
    matching a board's loaded pack against our records actually needs.

    Returns None when any part is missing, rather than a name with a hole in
    it that would match nothing.
    """
    suffix = {"force": "f", "moment": "m"}.get((model_type or "").lower())
    if not (device_axf_id and job_timestamp and suffix) or run_number is None:
        return None
    return f"{device_axf_id}-{job_timestamp}-{run_number}-{suffix}"


class Devkit(Base):
    __tablename__ = "devkits"

    # Not a ForeignKey to devices.axf_id, deliberately: a unit can be built
    # and recorded before it has ever submitted a training job (same reasoning
    # as ForcePlate.device_axf_id).
    device_axf_id = Column(String, primary_key=True)
    device_type_id = Column(String, nullable=False, index=True)  # '18'

    # --- flex board ---
    flex_fp = Column(String, nullable=True, index=True)
    tmp_uid = Column(String, nullable=True, index=True)
    # --- base board ---
    mcu_uid = Column(String, nullable=True, index=True)

    board_rev = Column(String, nullable=True)      # e.g. '0.0.1', '0.0.2'
    firmware_version = Column(String, nullable=True)
    model_version = Column(String, nullable=True)

    initialized_at = Column(DateTime(timezone=True), nullable=False,
                            default=current_time)
    assembled_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=current_time, onupdate=current_time)

    notes = Column(Text, nullable=True)
    source = Column(String, nullable=True)  # provenance, e.g. 'eol-fixture'

    assignment_history = relationship(
        "DevkitAssignmentHistory",
        back_populates="devkit",
        cascade="all, delete-orphan",
        lazy="select",
    )

    # --- writing ---------------------------------------------------------
    #
    # The three identifier columns are a FINGERPRINT, not the serial, so a
    # change to one is a board swap and gets an audit row rather than an
    # overwrite. `_set_identifier` is the only way they move.

    _IDENTIFIER_BOARDS = {"flex_fp": "flex", "tmp_uid": "flex",
                          "mcu_uid": "base"}

    def _set_identifier(self, db: Session, field: str, new_value,
                        changed_by=None, reason=None) -> bool:
        """Move one identifier, appending a history row if it actually moves.

        Returns True when a swap was recorded. A value arriving as None is
        "not reported by this writer", NOT "the board was removed", so it
        never clears a column and never records a swap: the training path
        knows the fingerprint and nothing else, and a bundle upload knows all
        three. Either must be able to write without erasing the other's work.
        """
        if new_value is None:
            return False
        old = getattr(self, field)
        if old == new_value:
            return False
        setattr(self, field, new_value)
        if old is None:
            # First time this identifier is known. Not a swap: there is no
            # "before" to record, and logging one would read as though a
            # board had been replaced when it was merely first seen.
            return False
        db.add(DevkitAssignmentHistory(
            device_axf_id=self.device_axf_id,
            which=self._IDENTIFIER_BOARDS[field], field=field,
            old_value=old, new_value=new_value,
            changed_by=changed_by, reason=reason))
        logger.warning(
            f"Devkit {self.device_axf_id}: {field} changed {old} -> "
            f"{new_value}. A calibration model is only valid on the flex it "
            f"was trained against.")
        return True

    @classmethod
    def record_calibration(cls, db: Session, job, run_number,
                           *, source: str = "training") -> Optional["Devkit"]:
        """Upsert the devkits row for a dev kit whose model has just been trained.

        Called from `Run.complete` on the run that becomes the job's best,
        right next to `Device.update_best_metrics` — the same moment, for the
        same reason: that is when a device acquires the model it will ship
        with, and the devkits row is the record of which unit got it.

        Scoped to device type 18. Anything else returns None untouched: a
        force plate has no flex and no fingerprint, and `force_plates` is
        already its record.

        Writes the fingerprint from the device id, which for a dev kit IS the
        fingerprint (see `flex_fp_from_device_id`). `tmp_uid` and `mcu_uid`
        are left alone — nothing server-side has read the board at training
        time, and a writer that filled them with None would erase what a
        diagnostics-bundle upload had already established.

        Does NOT commit. The caller owns the transaction, exactly as
        `update_best_metrics` does; `complete_run` commits both together, so
        a failure leaves neither a half-written run nor a devkits row for a
        run that did not land.
        """
        device_axf_id = getattr(job, "device_axf_id", None)
        if not is_devkit_id(device_axf_id):
            return None

        flex_fp = flex_fp_from_device_id(device_axf_id)
        if flex_fp is None:
            # `18.00000000` is the UNIDENTIFIED sentinel, not a board. It
            # passes device-id validation everywhere in this workspace, which
            # is exactly why the check has to be here: a devkits row keyed on
            # it would attribute a real calibration to a unit that does not
            # exist, and nothing downstream would catch it.
            logger.warning(
                f"Refusing to record a calibration against {device_axf_id}: "
                f"that is the UNIDENTIFIED flex sentinel, not a device.")
            return None

        row = db.query(cls).filter(
            cls.device_axf_id == device_axf_id).first()
        if row is None:
            row = cls(device_axf_id=device_axf_id,
                      device_type_id=DEVKIT_TYPE_ID,
                      initialized_at=current_time(), source=source)
            db.add(row)

        row._set_identifier(db, "flex_fp", flex_fp,
                            changed_by=source,
                            reason="derived from the device id at training")

        artifact = model_artifact_name(
            device_axf_id, getattr(job, "timestamp", None),
            getattr(job, "model_type", None), run_number)
        if artifact:
            row.model_version = artifact
        row.updated_at = current_time()
        logger.info(
            f"Devkit {device_axf_id}: calibration recorded, model "
            f"{artifact or '(unnamed)'}")
        return row

    def to_dict(self):
        return {
            "device_axf_id": self.device_axf_id,
            "device_type_id": self.device_type_id,
            "flex_fp": self.flex_fp,
            "tmp_uid": self.tmp_uid,
            "mcu_uid": self.mcu_uid,
            "board_rev": self.board_rev,
            "firmware_version": self.firmware_version,
            "model_version": self.model_version,
            "initialized_at": self.initialized_at,
            "assembled_at": self.assembled_at,
            "updated_at": self.updated_at,
            "notes": self.notes,
            "source": self.source,
        }


class DevkitAssignmentHistory(Base):
    """Append-only record of a board swap.

    One row per change, holding what the identifier was before and after.
    `which` says which board moved ('flex' or 'base') so a query can answer
    "has this unit's flex ever been replaced" without diffing columns.
    """
    __tablename__ = "devkit_assignment_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_axf_id = Column(String, ForeignKey("devkits.device_axf_id"),
                           nullable=False, index=True)
    which = Column(String, nullable=False)          # 'flex' | 'base'
    field = Column(String, nullable=False)          # 'flex_fp' | 'tmp_uid' | 'mcu_uid'
    old_value = Column(String, nullable=True)
    new_value = Column(String, nullable=True)
    changed_at = Column(DateTime(timezone=True), nullable=False,
                        default=current_time)
    changed_by = Column(String, nullable=True)
    reason = Column(Text, nullable=True)

    devkit = relationship("Devkit", back_populates="assignment_history")


Index("ix_devkit_history_device_time",
      DevkitAssignmentHistory.device_axf_id,
      DevkitAssignmentHistory.changed_at)


# --- request/response shapes -------------------------------------------------

class DevkitRequest(BaseModel):
    device_axf_id: str
    device_type_id: str = "18"
    flex_fp: Optional[str] = None
    tmp_uid: Optional[str] = None
    mcu_uid: Optional[str] = None
    board_rev: Optional[str] = None
    firmware_version: Optional[str] = None
    model_version: Optional[str] = None
    assembled_at: Optional[datetime] = None
    notes: Optional[str] = None
    source: Optional[str] = None


class DevkitUpdateRequest(BaseModel):
    flex_fp: Optional[str] = None
    tmp_uid: Optional[str] = None
    mcu_uid: Optional[str] = None
    board_rev: Optional[str] = None
    firmware_version: Optional[str] = None
    model_version: Optional[str] = None
    assembled_at: Optional[datetime] = None
    notes: Optional[str] = None
    changed_by: Optional[str] = None
    reason: Optional[str] = None


class DevkitResponse(BaseModel):
    device_axf_id: str
    device_type_id: str
    flex_fp: Optional[str] = None
    tmp_uid: Optional[str] = None
    mcu_uid: Optional[str] = None
    board_rev: Optional[str] = None
    firmware_version: Optional[str] = None
    model_version: Optional[str] = None
    initialized_at: datetime
    assembled_at: Optional[datetime] = None
    updated_at: datetime
    notes: Optional[str] = None
    source: Optional[str] = None
    #: True when `flex_fp` is the UNIDENTIFIED sentinel. Carried explicitly so
    #: a consumer cannot miss it by reading `flex_fp` as an ordinary value.
    flex_unidentified: bool = False

    class Config:
        from_attributes = True


class DevkitAssignmentHistoryResponse(BaseModel):
    id: int
    which: str
    field: str
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    changed_at: datetime
    changed_by: Optional[str] = None
    reason: Optional[str] = None

    class Config:
        from_attributes = True


class DevkitCaptureResponse(BaseModel):
    """One calibration capture session that fed the training job."""
    bucket_session_id: int
    date_iso: str
    bucket_prefix: str
    kind: str                       # 'train' | 'test' | 'both'
    family: Optional[str] = None
    day_number: Optional[int] = None
    calibrator_name: Optional[str] = None
    location: Optional[str] = None
    total_files: int = 0
    outcome: Optional[str] = None


class DevkitCalibrationResponse(BaseModel):
    """The support answer: when, against what, which model, what metrics.

    One of these per (device, model_type) — a unit has a force model and may
    have a moment model, and they are separate training jobs with separate
    metrics. `calibrated_at` is the completion of the run that produced the
    model, which is the date that belongs on a calibration record; the job
    `timestamp` is the human-scannable job UID and is the integer operators
    read, never a formatted date.
    """
    model_type: str
    model_artifact: Optional[str] = None
    calibrated_at: Optional[datetime] = None
    job_id: str
    job_timestamp: int
    job_status: str
    run_number: int
    hostname: Optional[str] = None
    train_metrics: Optional[dict] = None
    val_metrics: Optional[dict] = None
    test_metrics: Optional[dict] = None
    captures: List[DevkitCaptureResponse] = []


class DevkitRecordResponse(BaseModel):
    """Everything support can say about one shipped unit, from its id alone."""
    device_axf_id: str
    known_device: bool
    #: The devkits row, when one exists. Absent means nothing has recorded
    #: this unit — which is itself the answer, and distinct from "calibrated
    #: but we lost the metrics".
    devkit: Optional[DevkitResponse] = None
    calibrations: List[DevkitCalibrationResponse] = []
    board_swaps: List[DevkitAssignmentHistoryResponse] = []
    #: Plain-language summary of the two lines above, for a support reply.
    summary: str
