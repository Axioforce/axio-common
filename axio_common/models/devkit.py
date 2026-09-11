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
from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel
from sqlalchemy import (
    Column, String, Text, DateTime, Integer, ForeignKey, Index,
)
from sqlalchemy.orm import relationship

from axio_common.database import Base
from axio_common.utils.model_utils import current_time


#: The firmware's "fingerprint did not form" sentinel (PROTOCOL.md).
UNIDENTIFIED_FLEX_FP = "00000000"


def unidentified_flex(flex_fp: Optional[str]) -> bool:
    """True when a reported fingerprint is the UNIDENTIFIED sentinel.

    Call this before persisting or joining on a fingerprint. A capture or a
    provisioning record carrying this value is unattributable, not attributed
    to a board named zero.
    """
    return not flex_fp or flex_fp.strip().lower() == UNIDENTIFIED_FLEX_FP


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
