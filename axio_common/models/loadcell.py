"""
Six-axis load cell unit records (ring0 board, device types `19`, `1a`..`1f`).

Contract 6, `axioforce-loadcell-firmware/docs/SERVER_RECORDS.md` (work items
#1367 / #1380). What FlyNNServer stores about a load cell, from the first
bench capture to the calibrated product.

The facts this module holds, and why each helper exists:

  - **A board's identity is its serial**: CRC-32 (IEEE, zlib's) over the 12
    STM32 UID bytes in the order IDENTIFY reports them (word 0 first, each
    word little-endian), spelled as 8 lowercase hex. The UID itself is those
    12 bytes as 24 lowercase hex, in the same order. `format_serial()` and
    `format_mcu_uid()` are the one spelling of each; `serial_from_uid()` lets
    the server **verify** a reported serial instead of trusting it.

    Note the contrast with the dev kit (`devkit.py`), whose `mcu_uid` is
    24 UPPERCASE hex in the firmware's `flexid` order (w2 w1 w0, big-endian).
    The two families spell the same kind of value differently on purpose:
    here the serial is a CRC over the raw IDENTIFY bytes, so the stored UID
    is exactly those bytes and the CRC can be recomputed from the column.
    Never pass a load cell UID through the dev kit's `format_mcu_uid`.

  - **The device type is unknown until calibration.** The calibrator picks
    it (`1a`..`1f`, or `19` for a custom cell), the calibration software
    checks it against the measured pad stiffness, and it rides in the
    installed model. Before that the unit is keyed by the serial alone and
    nothing is renamed later. IDENTIFY reports type `0` for an uncalibrated
    board: that is a **sentinel, not a type**, and `device_id_for()` returns
    None for it, so `00.<serial>` can never become a device id.

  - **CRC-32 over 96 bits can collide.** `LoadCellUnit.register()` refuses a
    serial already held by a different UID (`LoadCellIdentityConflict`), and
    a UID already registered under a different serial. Nothing is written in
    either case.

  - **The type is set once**, through `LoadCellUnit.set_type()` only, which
    appends a `loadcell_unit_type_history` row. Changing it on a typed unit
    needs `force=True` and a reason (a re-calibration after a pad rework is
    the only reason there is), and the history keeps the old type.

`device_axf_id` (`<type>.<serial>`) is NOT a ForeignKey to `devices`, for the
same reason `Devkit`'s and `ForcePlate`'s are not: a unit exists long before
any training job does.
"""
import re
import zlib
from datetime import datetime
from typing import List, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Session, relationship

from axio_common.database import Base
from axio_common.logger import logger
from axio_common.utils.model_utils import current_time


#: The load cell's device types (firmware `docs/PROTOCOL.md` "Identity",
#: reserved 2026-11-06, #1367): `1a`/`1b`/`1c` center hole, low /
#: intermediate / high range; `1d`/`1e`/`1f` no hole, same order; `19`
#: custom load cells. Never "uncalibrated": that is type 0, which is no type.
LOADCELL_TYPE_IDS = ("19", "1a", "1b", "1c", "1d", "1e", "1f")

#: Display names for the `devices` row a typed unit gets. The product names
#: are TBD (PLAN.md); these describe the pad, as the PROTOCOL.md table does.
LOADCELL_TYPE_NAMES = {
    "19": "Load Cell - Custom",
    "1a": "Load Cell - Center Hole, Low Range",
    "1b": "Load Cell - Center Hole, Intermediate Range",
    "1c": "Load Cell - Center Hole, High Range",
    "1d": "Load Cell - No Hole, Low Range",
    "1e": "Load Cell - No Hole, Intermediate Range",
    "1f": "Load Cell - No Hole, High Range",
}

#: IDENTIFY's `device_type` for a board with no calibration. Not a type.
UNCALIBRATED_TYPE = "00"

#: The 8 MLX90394 magnetometers, in bus order.
LOADCELL_N_MAG = 8

_HEX8_RE = re.compile(r"^[0-9a-f]{8}$")
_HEX24_RE = re.compile(r"^[0-9a-f]{24}$")
_TYPE_RE = re.compile(r"^[0-9a-f]{2}$")
_MODEL_VERSION_RE = re.compile(r"^[0-9]{8}$")


# --- spellings ---------------------------------------------------------------

def _hex32(value, what: str) -> str:
    """8 lowercase hex from a 32-bit int or an 8-hex string; ValueError else."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"not a {what}: {value!r}")
    if isinstance(value, int):
        if not 0 <= value <= 0xFFFFFFFF:
            raise ValueError(f"{what} {value!r} is not a 32-bit value")
        return f"{value:08x}"
    if isinstance(value, str):
        text = value.strip().lower()
        if not _HEX8_RE.match(text):
            raise ValueError(
                f"{what} {value!r} is not 8 hex digits; it is a CRC-32 and "
                f"is always spelled with all 8, leading zeros included")
        return text
    raise ValueError(f"not a {what}: {value!r}")


def format_serial(value) -> str:
    """Canonical serial: 8 lowercase hex, from an int or a string.

        format_serial(0xFE059E4B)  -> "fe059e4b"
        format_serial("FE059E4B")  -> "fe059e4b"
        format_serial(0x1234)      -> "00001234"   (never "1234")

    Raises ValueError on None, a bool, a negative or >32-bit int, or a string
    that is not exactly 8 hex digits, rather than truncating or padding it
    into something that looks valid.
    """
    return _hex32(value, "load cell serial")


def format_mcu_uid(value) -> str:
    """Canonical load cell MCU UID: 24 lowercase hex, IDENTIFY's byte order.

    - bytes (12): the UID exactly as IDENTIFY carries it (word 0 first, each
      word little-endian) -> `.hex()`.
    - str (24 hex, any case): taken to be already in that order.
    - int: the 96-bit number whose 24-hex spelling is the canonical string
      (`f"{value:024x}"`), i.e. the first IDENTIFY byte is the most
      significant.

    This is NOT the dev kit's `format_mcu_uid` (uppercase, `flexid` order).
    Raises ValueError on None, a bool, a wrong length or a non-hex string.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"not a load cell MCU UID: {value!r}")
    if isinstance(value, (bytes, bytearray)):
        if len(value) != 12:
            raise ValueError(
                f"load cell MCU UID is 12 bytes, got {len(value)}")
        return bytes(value).hex()
    if isinstance(value, int):
        if not 0 <= value < (1 << 96):
            raise ValueError(f"load cell MCU UID {value!r} is not 96 bits")
        return f"{value:024x}"
    if isinstance(value, str):
        text = value.strip().lower()
        if not _HEX24_RE.match(text):
            raise ValueError(
                f"load cell MCU UID {value!r} is not 24 hex digits")
        return text
    raise ValueError(f"not a load cell MCU UID: {value!r}")


def unread_mcu_uid(uid_hex: str) -> bool:
    """True for an all-zero UID: a read that failed, never a board.

    A zero-filled UID would CRC to a perfectly ordinary-looking serial, so it
    is refused at registration instead of becoming a unit.
    """
    return set(uid_hex) == {"0"}


def serial_from_uid(uid_hex) -> str:
    """The serial a UID must have: zlib's CRC-32 over the 12 UID bytes.

    The bytes are taken in the order IDENTIFY reports them (word 0 first,
    little-endian), which is the order `format_mcu_uid` stores. Accepts
    anything `format_mcu_uid` accepts.
    """
    uid = format_mcu_uid(uid_hex)
    return f"{zlib.crc32(bytes.fromhex(uid)) & 0xFFFFFFFF:08x}"


def _type_text(type_id) -> Optional[str]:
    """2 lowercase hex for an int or str type, or None if it is not one."""
    if isinstance(type_id, bool) or type_id is None:
        return None
    if isinstance(type_id, int):
        return f"{type_id:02x}" if 0 <= type_id <= 0xFF else None
    if isinstance(type_id, str):
        text = type_id.strip().lower()
        if text.startswith("0x"):
            text = text[2:]
        if len(text) == 1:
            text = "0" + text
        return text if _TYPE_RE.match(text) else None
    return None


def is_loadcell_type(type_id) -> bool:
    """True for `19` and `1a`..`1f` (str in any case, or the int). Never raises.

    False for the uncalibrated sentinel 0, which is no type at all.
    """
    return _type_text(type_id) in LOADCELL_TYPE_IDS


def format_type_id(type_id) -> str:
    """Canonical load cell type (`"1a"`); ValueError for anything else.

    Refuses the uncalibrated sentinel 0 and every non-load-cell type.
    """
    text = _type_text(type_id)
    if text not in LOADCELL_TYPE_IDS:
        raise ValueError(
            f"{type_id!r} is not a load cell device type "
            f"({', '.join(LOADCELL_TYPE_IDS)})")
    return text


def device_id_for(serial, type_id) -> Optional[str]:
    """`<type>.<serial>` for a typed unit; None while it has no type.

    None, `0` and `"00"` (IDENTIFY's "uncalibrated") give None: a unit with no
    type has no device id, and the sentinel must never be minted into one.
    Any other type that is not a load cell type raises ValueError, as does a
    malformed serial.
    """
    serial = format_serial(serial)
    if type_id is None or _type_text(type_id) == UNCALIBRATED_TYPE:
        return None
    return f"{format_type_id(type_id)}.{serial}"


def format_model_version(value) -> Optional[str]:
    """A pack's YYYYMMDD as 8 digits (int or str); None for None."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"not a model version: {value!r}")
    text = str(value).strip()
    if not _MODEL_VERSION_RE.match(text):
        raise ValueError(f"model version {value!r} is not YYYYMMDD")
    return text


def format_model_crc32(value) -> Optional[str]:
    """A pack's header CRC-32 as 8 lowercase hex; None for None."""
    if value is None:
        return None
    return _hex32(value, "model crc32")


# --- errors ------------------------------------------------------------------

class LoadCellSerialMismatch(ValueError):
    """Registration: the reported serial is not the CRC of the reported UID
    (rule 2, HTTP 400). The client computed it wrong or sent another board's
    UID."""


class LoadCellIdentityConflict(Exception):
    """Registration: the serial is held by another UID, or the UID by another
    serial (rules 3 and 4, HTTP 409). Nothing was written."""

    def __init__(self, message: str, *, serial: str, mcu_uid: str,
                 existing_serial: str, existing_mcu_uid: str):
        super().__init__(message)
        self.serial = serial
        self.mcu_uid = mcu_uid
        self.existing_serial = existing_serial
        self.existing_mcu_uid = existing_mcu_uid


class LoadCellTypeAlreadySet(Exception):
    """`set_type`: the unit already has a different type and the call was not
    forced (HTTP 409). Nothing was written."""

    def __init__(self, message: str, *, serial: str, current_type: str,
                 requested_type: str):
        super().__init__(message)
        self.serial = serial
        self.current_type = current_type
        self.requested_type = requested_type


# --- tables ------------------------------------------------------------------

#: The optional fields registration and PATCH may fill. Never the serial, the
#: UID, the type, the device id or the installed model.
REGISTER_FIELDS = ("board_rev", "firmware_version", "mag_ids", "notes",
                   "source")
PATCH_FIELDS = ("board_rev", "firmware_version", "mag_ids", "pad_lot",
                "calibration_date", "operator", "notes", "source")


class LoadCellUnit(Base):
    __tablename__ = "loadcell_units"
    __table_args__ = (
        UniqueConstraint("mcu_uid", name="uq_loadcell_units_mcu_uid"),
        UniqueConstraint("device_axf_id",
                         name="uq_loadcell_units_device_axf_id"),
        Index("ix_loadcell_units_device_type_id", "device_type_id"),
    )

    #: 8 lowercase hex, CRC-32 over the UID. The key forever: records before
    #: calibration are keyed by it and nothing is renamed later.
    serial = Column(String, primary_key=True)
    #: 24 lowercase hex, IDENTIFY order. UNIQUE: a CRC collision is refused at
    #: registration, and the constraint backs the check.
    mcu_uid = Column(String, nullable=False)
    #: `19`, `1a`..`1f`; NULL until calibration. Moved only by `set_type()`.
    device_type_id = Column(String, nullable=True)
    #: `<type>.<serial>` once typed, never before. UNIQUE, not a FK.
    device_axf_id = Column(String, nullable=True)

    #: Operator-entered ("A"): IDENTIFY's board_rev is an enum, not a label.
    board_rev = Column(String, nullable=True)
    firmware_version = Column(String, nullable=True)
    #: The installed pack's YYYYMMDD, once calibrated (set with the type).
    model_version = Column(String, nullable=True)
    #: The installed pack's header CRC-32, 8 hex (set with the type).
    model_crc32 = Column(String, nullable=True)
    #: Operator-entered pad lot. Never the pad material, brand or grade.
    pad_lot = Column(String, nullable=True)
    #: The 8 MLX90394 ids in bus order, if readable.
    mag_ids = Column(JSON, nullable=True)
    #: The date on the certificate.
    calibration_date = Column(DateTime(timezone=True), nullable=True)
    operator = Column(String, nullable=True)

    #: First registration; immutable.
    initialized_at = Column(DateTime(timezone=True), nullable=False,
                            default=current_time)
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=current_time, onupdate=current_time)
    notes = Column(Text, nullable=True)
    #: Provenance, e.g. `lc.py register`.
    source = Column(String, nullable=True)

    type_history = relationship(
        "LoadCellUnitTypeHistory",
        back_populates="unit",
        cascade="all, delete-orphan",
        order_by="LoadCellUnitTypeHistory.id",
        lazy="select",
    )

    @classmethod
    def register(cls, db: Session, serial, mcu_uid, *, board_rev=None,
                 firmware_version=None, mag_ids=None, notes=None,
                 source=None) -> Tuple["LoadCellUnit", bool]:
        """Register a board: idempotent upsert keyed by serial.

        The rules of contract 6, in order:
          1. both values in their canonical spelling (ValueError on a
             malformed one; an all-zero UID is a failed read, also refused);
          2. the serial must be `serial_from_uid(mcu_uid)`
             (`LoadCellSerialMismatch`);
          3. a row with this serial and another UID is the CRC collision
             (`LoadCellIdentityConflict`);
          4. a row with this UID under another serial, likewise (cannot
             happen after rule 2, but the constraint is checked, not
             assumed);
          5. otherwise create the row, or fill the optional fields that were
             sent (None erases nothing). The type is never set here.

        Returns `(row, created)`. Does NOT commit; nothing is added to the
        session when a rule refuses.
        """
        serial = format_serial(serial)
        uid = format_mcu_uid(mcu_uid)
        if unread_mcu_uid(uid):
            raise ValueError(
                "load cell MCU UID is all zeros: a read that failed, not a "
                "board")
        expected = serial_from_uid(uid)
        if expected != serial:
            raise LoadCellSerialMismatch(
                f"serial does not match the UID: {uid} has serial "
                f"{expected}, not {serial}")

        row = db.query(cls).filter(cls.serial == serial).first()
        if row is not None and row.mcu_uid != uid:
            raise LoadCellIdentityConflict(
                f"serial already registered to another board: serial "
                f"{serial} is held by UID {row.mcu_uid}; this board's UID is "
                f"{uid} (a CRC-32 collision)",
                serial=serial, mcu_uid=uid,
                existing_serial=row.serial, existing_mcu_uid=row.mcu_uid)
        other = db.query(cls).filter(cls.mcu_uid == uid,
                                     cls.serial != serial).first()
        if other is not None:
            raise LoadCellIdentityConflict(
                f"UID already registered under another serial: UID {uid} is "
                f"unit {other.serial}, and its serial is {serial}",
                serial=serial, mcu_uid=uid,
                existing_serial=other.serial,
                existing_mcu_uid=other.mcu_uid)

        values = {"board_rev": board_rev,
                  "firmware_version": firmware_version,
                  "mag_ids": _check_mag_ids(mag_ids),
                  "notes": notes, "source": source}
        created = row is None
        now = current_time()
        if created:
            row = cls(serial=serial, mcu_uid=uid, initialized_at=now,
                      updated_at=now)
            db.add(row)
        for field, value in values.items():
            if value is not None:
                setattr(row, field, value)
        row.updated_at = now
        logger.info(f"Load cell {serial}: "
                    f"{'registered' if created else 're-registered'} "
                    f"(UID {uid})")
        return row, created

    def update_fields(self, **fields) -> None:
        """Fill or correct the operator fields (PATCH). None erases nothing.

        Refuses anything outside `PATCH_FIELDS`: the serial, the UID, the
        type, the device id and the installed model never move here.
        """
        unknown = set(fields) - set(PATCH_FIELDS)
        if unknown:
            raise ValueError(
                f"not editable on a load cell unit: {sorted(unknown)}")
        if "mag_ids" in fields:
            fields["mag_ids"] = _check_mag_ids(fields["mag_ids"])
        for field, value in fields.items():
            if value is not None:
                setattr(self, field, value)
        self.updated_at = current_time()

    def set_type(self, db: Session, type_id, *, changed_by: str,
                 reason: Optional[str] = None, force: bool = False,
                 model_version=None, model_crc32=None) -> bool:
        """The one way a unit's type moves. Appends a history row on a change.

        - untyped unit: sets the type and `device_axf_id`, history row with
          `previous_type` NULL;
        - the same type again: no history row; records the installed model
          fields that were sent (a re-install of the same type's model);
        - a different type: `LoadCellTypeAlreadySet` unless `force=True`
          with a reason, and then the history keeps the old type.

        Returns True when the type changed. ValueError for a type that is not
        a load cell type, a missing `changed_by`, a forced change without a
        reason, or a malformed model field. Does NOT commit.
        """
        new_type = format_type_id(type_id)
        if not (changed_by or "").strip():
            raise ValueError("changed_by is required to set a type")
        version = format_model_version(model_version)
        crc = format_model_crc32(model_crc32)
        old_type = self.device_type_id

        if old_type is not None and old_type != new_type:
            if not force:
                raise LoadCellTypeAlreadySet(
                    f"load cell {self.serial} is already type {old_type}; "
                    f"setting {new_type} needs force with a reason (a "
                    f"re-calibration after a pad rework)",
                    serial=self.serial, current_type=old_type,
                    requested_type=new_type)
            if not (reason or "").strip():
                raise ValueError(
                    "a forced type change needs a reason (a re-calibration "
                    "after a pad rework is the only reason there is)")

        if version is not None:
            self.model_version = version
        if crc is not None:
            self.model_crc32 = crc
        self.updated_at = current_time()
        if old_type == new_type:
            return False

        self.device_type_id = new_type
        self.device_axf_id = device_id_for(self.serial, new_type)
        db.add(LoadCellUnitTypeHistory(
            serial=self.serial, previous_type=old_type, new_type=new_type,
            changed_by=changed_by, reason=reason))
        if old_type is None:
            logger.info(f"Load cell {self.serial}: type set to {new_type} "
                        f"({self.device_axf_id})")
        else:
            logger.warning(
                f"Load cell {self.serial}: type FORCED {old_type} -> "
                f"{new_type} by {changed_by}: {reason}")
        return True


class LoadCellUnitTypeHistory(Base):
    """Append-only: every type a unit has been given, written by `set_type`.

    Nothing here is ever updated or deleted; a forced re-type keeps the old
    type on file.
    """
    __tablename__ = "loadcell_unit_type_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    serial = Column(String, ForeignKey("loadcell_units.serial"),
                    nullable=False, index=True)
    previous_type = Column(String, nullable=True)
    new_type = Column(String, nullable=False)
    changed_by = Column(String, nullable=True)
    reason = Column(Text, nullable=True)
    changed_at = Column(DateTime(timezone=True), nullable=False,
                        default=current_time)

    unit = relationship("LoadCellUnit", back_populates="type_history")


Index("ix_loadcell_unit_type_history_serial_time",
      LoadCellUnitTypeHistory.serial, LoadCellUnitTypeHistory.changed_at)


def _check_mag_ids(mag_ids):
    """None, or a list of the 8 magnetometer ids (an entry may be None when
    that die did not answer)."""
    if mag_ids is None:
        return None
    if not isinstance(mag_ids, (list, tuple)) or \
            len(mag_ids) != LOADCELL_N_MAG:
        raise ValueError(
            f"mag_ids is the {LOADCELL_N_MAG} MLX90394 ids in bus order")
    for v in mag_ids:
        if v is not None and (isinstance(v, bool)
                              or not isinstance(v, (str, int))):
            raise ValueError(f"not a magnetometer id: {v!r}")
    return list(mag_ids)


# --- API shapes --------------------------------------------------------------

MagId = Optional[Union[str, int]]


class LoadCellRegisterRequest(BaseModel):
    """Body of `POST /loadcell/units`."""
    serial: Union[str, int]
    mcu_uid: Union[str, int]
    board_rev: Optional[str] = None
    firmware_version: Optional[str] = None
    mag_ids: Optional[List[MagId]] = None
    notes: Optional[str] = None
    source: Optional[str] = None


class LoadCellUnitUpdateRequest(BaseModel):
    """Body of `PATCH /loadcell/units/{serial}`. Unknown fields (the serial,
    the UID, the type, the model) are refused, not ignored."""
    model_config = ConfigDict(extra="forbid")

    board_rev: Optional[str] = None
    firmware_version: Optional[str] = None
    mag_ids: Optional[List[MagId]] = None
    pad_lot: Optional[str] = None
    calibration_date: Optional[datetime] = None
    operator: Optional[str] = None
    notes: Optional[str] = None
    source: Optional[str] = None


class LoadCellSetTypeRequest(BaseModel):
    """Body of `PUT /loadcell/units/{serial}/type`."""
    # `model_*` fields are the pack's, not pydantic's namespace.
    model_config = ConfigDict(protected_namespaces=())

    device_type_id: Union[str, int]
    changed_by: str
    reason: Optional[str] = None
    force: bool = False
    model_version: Optional[Union[str, int]] = None
    model_crc32: Optional[Union[str, int]] = None


class LoadCellUnitResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    serial: str
    mcu_uid: str
    device_type_id: Optional[str] = None
    device_axf_id: Optional[str] = None
    board_rev: Optional[str] = None
    firmware_version: Optional[str] = None
    model_version: Optional[str] = None
    model_crc32: Optional[str] = None
    pad_lot: Optional[str] = None
    mag_ids: Optional[List[MagId]] = None
    calibration_date: Optional[datetime] = None
    operator: Optional[str] = None
    initialized_at: datetime
    updated_at: datetime
    notes: Optional[str] = None
    source: Optional[str] = None


class LoadCellUnitTypeHistoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    serial: str
    previous_type: Optional[str] = None
    new_type: str
    changed_by: Optional[str] = None
    reason: Optional[str] = None
    changed_at: datetime
