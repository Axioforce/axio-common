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

DECIDED 2026-09-12 (Stephen) -- **the device id is derived FROM the flex**
(`18.<flex_fp as 8 lowercase hex>`), which supersedes the earlier
"administratively assigned id with fingerprints recorded against it". The
consequence runs through this whole module: the flex IS the unit's identity,
so swapping the flex produces a **different device id** (a new unit), while
swapping the base board is the same unit with a new `mcu_uid`. Mint ids only
through `device_id_from_flex_fp()`, and spell fingerprints only through
`format_flex_fp()`.

`flex_fp = '00000000'` is the firmware's UNIDENTIFIED sentinel, not a real
board — a unit whose fingerprint did not form. It must never be stored as
though it were an identity; `unidentified_flex()` exists to make that check
hard to forget, since `18.00000000` otherwise passes the workspace's
device-id validators and looks legitimate.
"""
import re
from datetime import datetime
from typing import Optional, List

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, JSON,
    String, Text,
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


def format_flex_fp(value) -> Optional[str]:
    """Canonical 8-lowercase-hex spelling of a flex fingerprint, from int or str.

    This exists because of one specific, already-observed foot-gun. The SDK
    carries the fingerprint as an **int** and its UNIDENTIFIED sentinel is
    `0`; this schema carries it as the **string** `"00000000"`. A writer that
    reaches for the obvious `str(flex_fp)` turns the sentinel into `"0"` --
    and `unidentified_flex("0")` is **False**, so the one value that means
    "this unit has no identity" sails past the guard written to catch it and
    gets stored as though it were a board. `"18.0"` then looks like a device.

    So there is exactly one way to spell a fingerprint, and it is this:

        format_flex_fp(0)            -> "00000000"      (the sentinel, intact)
        format_flex_fp(0x5fdb3916)   -> "5fdb3916"
        format_flex_fp("5FDB3916")   -> "5fdb3916"
        format_flex_fp(None)         -> None

    Returning the sentinel rather than None is deliberate: "we read this unit
    and its fingerprint did not form" is a fact worth recording. Use
    `device_id_from_flex_fp()` when you need an *identity*, which the
    sentinel can never be.

    Raises ValueError on anything that is not a fingerprint -- a negative
    int, an out-of-range int, or a string that is not 8 hex digits -- rather
    than truncating it into something that looks valid.
    """
    if value is None:
        return None
    if isinstance(value, bool):                     # bool is an int subclass
        raise ValueError(f"not a flex fingerprint: {value!r}")
    if isinstance(value, int):
        if not 0 <= value <= 0xFFFFFFFF:
            raise ValueError(
                f"flex fingerprint {value!r} is not a 32-bit CRC value")
        return f"{value:08x}"
    if isinstance(value, str):
        text = value.strip().lower()
        if len(text) != 8 or any(c not in "0123456789abcdef" for c in text):
            raise ValueError(
                f"flex fingerprint {value!r} is not 8 hex digits; it is a "
                f"CRC-32 and is always spelled with all 8, leading zeros "
                f"included")
        return text
    raise ValueError(f"not a flex fingerprint: {value!r}")


def device_id_from_flex_fp(value) -> Optional[str]:
    """`18.<8 hex>` for a real fingerprint; None for the sentinel or None.

    The inverse of `flex_fp_from_device_id`, and the only place a dev kit's
    device id should be minted. Per the DECIDED block of 2026-09-12
    (Stephen): **the id is derived from the flex**, so a fingerprint is all
    that is needed to name the unit -- and the sentinel is therefore the one
    input that must NOT produce an id, because `18.00000000` passes every
    device-id validator in this workspace while naming nothing.
    """
    fp = format_flex_fp(value)
    if fp is None or unidentified_flex(fp):
        return None
    return f"{DEVKIT_TYPE_ID}.{fp}"


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


#: The TMP118 id a flex reports when the sensor did not answer. The SDK
#: zero-fills it on a failed read (firmware `bcmd.c`), so all zeros is "not
#: read", never an id.
UNIDENTIFIED_TMP_UID = "000000000000"

_TMP_UID_RE = re.compile(r"^[0-9A-F]{12}$")
_MCU_UID_RE = re.compile(r"^[0-9A-F]{24}$")


def format_tmp_uid(value) -> Optional[str]:
    """Canonical `21E8A3E64CCF`: the TMP118's 48-bit id, 12 uppercase hex.

    The spelling the firmware's `flexid` reply prints and `axio-devkit eol`
    records (`eol.tmp_uid_str`). Accepts that string in any case, or the SDK's
    6 raw bytes (same byte order, only the case differs). Returns None for
    None and raises ValueError for anything that is not 12 hex digits.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        if len(value) != 6:
            raise ValueError(f"tmp_uid is 6 bytes, got {len(value)}")
        return bytes(value).hex().upper()
    if isinstance(value, str):
        text = value.strip().upper()
        if not _TMP_UID_RE.match(text):
            raise ValueError(f"tmp_uid {value!r} is not 12 hex digits")
        return text
    raise ValueError(f"not a tmp_uid: {value!r}")


def format_mcu_uid(value) -> Optional[str]:
    """Canonical `0037324E4236501000340020`: the STM32 UID, 24 uppercase hex.

    **Byte order is the trap here.** The firmware's text `flexid` reply and
    the USB serial string print the three UID words big-endian in the order
    w2, w1, w0; the binary IDENTIFY reply carries them little-endian w0, w1,
    w2, so the SDK's raw `mcu_uid.hex()` is the full byte reversal of the
    string on every board ledger. A 24-hex string cannot say which order it
    is in, so strings are taken to be in the `flexid` / USB-serial order --
    what AxioTDK parses and what `axio-devkit eol` records (`eol.mcu_uid_str`)
    -- and raw IDENTIFY bytes are converted exactly as `eol.mcu_uid_str` does.
    Never hand this a `.hex()` of the IDENTIFY bytes as a string.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        if len(raw) != 12:
            raise ValueError(f"mcu_uid is 12 bytes, got {len(raw)}")
        w0 = int.from_bytes(raw[0:4], "little")
        w1 = int.from_bytes(raw[4:8], "little")
        w2 = int.from_bytes(raw[8:12], "little")
        return f"{w2:08X}{w1:08X}{w0:08X}"
    if isinstance(value, str):
        text = value.strip().upper()
        if not _MCU_UID_RE.match(text):
            raise ValueError(f"mcu_uid {value!r} is not 24 hex digits")
        return text
    raise ValueError(f"not an mcu_uid: {value!r}")


def _all_zero(text: Optional[str]) -> bool:
    return bool(text) and set(text) == {"0"}


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

    #: OPERATOR-ENTERED. The board revision is **not obtainable from the
    #: device**: `bcmd.c` reports `0.0.0` for every board, honestly, because
    #: nothing on the hardware encodes it. So this is typed by whoever
    #: provisioned the unit and is the field in this table most likely to
    #: rot. Treat a value here as a human's claim, never as a reading.
    board_rev = Column(String, nullable=True)      # e.g. '0.0.1', '0.0.2'
    firmware_version = Column(String, nullable=True)
    model_version = Column(String, nullable=True)

    # --- provisioning record (AXI-14) ------------------------------------
    #
    # What the unit was when it shipped, as recorded by whoever built and
    # tested it. None of this is derivable from anything else in the
    # database, which is why it lives on the row rather than being joined.

    #: When this unit was calibrated, as the provisioning record states it.
    #: NOT the same fact as the training run's `completed_at`, which the
    #: calibration endpoint joins out of jobs/runs -- that is when the model
    #: was *trained*, this is the date that goes on the certificate. They are
    #: usually the same day and are allowed to differ; when they do, the
    #: joined run date is the one with evidence behind it.
    calibration_date = Column(DateTime(timezone=True), nullable=True)

    #: Who provisioned and end-of-line tested the unit. Free text: the name
    #: an operator signs a certificate with, not a foreign key to anything.
    operator = Column(String, nullable=True)

    #: Magnet / pad lot the flex was built from. Operator-entered.
    magnet_lot = Column(String, nullable=True)

    #: The five HAL3304 per-die traceability strings, bus 1..5 in order, as
    #: read off the board (`mag id` -> `raw=`), e.g.
    #: `["A1B2C3D4E5F6", ...]`.
    #:
    #: Stored raw, and stored **in addition to** `flex_fp` rather than
    #: instead of it, for one reason: `flex_fp` is a CRC-32 over exactly
    #: these bytes and **you cannot invert a CRC**. Keeping only the
    #: fingerprint means that "which magnet lot / wafer is in unit X" -- the
    #: question a field failure asks -- is unanswerable for every unit
    #: already shipped. Bus order matters here as much as it does in the
    #: fingerprint: the value identifies sensor *placement*.
    mag_ids = Column(JSON, nullable=True)

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
        if field == "flex_fp":
            # Under the flex-derived id rule (DECIDED 2026-09-12) the row's
            # own key is `18.<flex_fp>`, so a fingerprint that no longer
            # matches the id means the record has stopped describing the unit
            # it is named after. That is a data-entry error or a genuinely
            # different unit, never a routine swap -- say so, loudly, rather
            # than filing it next to base-board changes as though it were one.
            expected = device_id_from_flex_fp(new_value)
            if expected and expected != self.device_axf_id:
                logger.error(
                    f"Devkit {self.device_axf_id}: flex_fp now reads "
                    f"{new_value}, which derives the device id {expected}. "
                    f"The id comes FROM the flex, so this row no longer "
                    f"describes the unit it is keyed under -- the new flex "
                    f"is {expected}, a different device, and this record "
                    f"should be corrected rather than carried forward.")
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

    @classmethod
    def record_identity(cls, db: Session, device_axf_id: str, *,
                        flex_fp=None, tmp_uid=None, mcu_uid=None,
                        source: str, reason: Optional[str] = None
                        ) -> Optional["Devkit"]:
        """Learn which boards are in a dev kit, BEFORE its EOL test (WI-547).

        The training path can only know the fingerprint, because the device id
        IS the fingerprint. `tmp_uid` (the flex) and `mcu_uid` (the base
        board) come off the board itself, and they have to be on the unit
        record before EOL runs: `shippability()` holds an EOL result to the
        identifiers on the record, and a record that never learned them has
        nothing to hold it to -- a base-board swap between calibration and
        EOL would go unseen.

        Scoped to device type 18. Anything else returns None untouched, as
        does `record_calibration`. The UNIDENTIFIED sentinel is refused as a
        device id (returns None, logs a warning), and an all-zero tmp_uid or
        mcu_uid is a read that failed, not an id: it is ignored rather than
        stored. A `flex_fp` that does not match the id is a report from a
        different unit and raises ValueError, as does a malformed uid.

        Creates the row when there is none -- this is typically the first
        write a unit gets, at its calibration session -- and moves each
        identifier through `_set_identifier`, so a value that changes is an
        audited board swap and a None never erases anything. Does NOT commit;
        the caller owns the transaction.
        """
        if not is_devkit_id(device_axf_id):
            return None
        m = _DEVICE_ID_RE.match(device_axf_id.strip())
        axf = f"{DEVKIT_TYPE_ID}.{m.group(2).lower()}"
        id_fp = flex_fp_from_device_id(axf)
        if id_fp is None:
            logger.warning(
                f"Refusing to record board identity against {device_axf_id}: "
                f"that is the UNIDENTIFIED flex sentinel, not a device.")
            return None

        reported_fp = format_flex_fp(flex_fp)
        if reported_fp is not None and reported_fp != id_fp:
            raise ValueError(
                f"flex_fp {reported_fp} does not match {axf}: the id is "
                f"derived from the fingerprint, so this report describes "
                f"{device_id_from_flex_fp(reported_fp) or 'no unit'}")
        tmp = format_tmp_uid(tmp_uid)
        mcu = format_mcu_uid(mcu_uid)
        if _all_zero(tmp):
            logger.warning(f"Devkit {axf}: tmp_uid reported as all zeros "
                           f"(the TMP118 did not answer); not stored.")
            tmp = None
        if _all_zero(mcu):
            logger.warning(f"Devkit {axf}: mcu_uid reported as all zeros; "
                           f"not stored.")
            mcu = None

        row = db.query(cls).filter(cls.device_axf_id == axf).first()
        if row is None:
            row = cls(device_axf_id=axf, device_type_id=DEVKIT_TYPE_ID,
                      initialized_at=current_time(), source=source)
            db.add(row)
        why = reason or f"reported by {source}"
        row._set_identifier(db, "flex_fp", id_fp, changed_by=source,
                            reason=why)
        row._set_identifier(db, "tmp_uid", tmp, changed_by=source, reason=why)
        row._set_identifier(db, "mcu_uid", mcu, changed_by=source, reason=why)
        row.updated_at = current_time()
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
            "calibration_date": self.calibration_date,
            "operator": self.operator,
            "magnet_lot": self.magnet_lot,
            "mag_ids": self.mag_ids,
            "initialized_at": self.initialized_at,
            "assembled_at": self.assembled_at,
            "updated_at": self.updated_at,
            "notes": self.notes,
            "source": self.source,
        }


class DevkitAssignmentHistory(Base):
    """Append-only record of a board swap, under a flex-derived device id.

    One row per change, holding what the identifier was before and after.
    `which` says which board moved ('flex' or 'base') so a query can answer
    "what has been replaced in this unit" without diffing columns.

    **What this table actually tracks: BASE-BOARD swaps.** Per the DECIDED
    block of 2026-09-12 (Stephen), the `18.xxxxxxxx` device id is derived
    from the flex fingerprint, so the flex IS the unit's identity. Under that
    rule a flex swap does not produce a history row on this unit -- it
    produces a **different device id**, i.e. a different unit, whose model
    and record travel with it. What can change while the id stays put is the
    base board, and `mcu_uid` is what records it.

    This is the reverse of what an earlier draft assumed (an administratively
    assigned id with all three fingerprints recorded against it, so the unit
    kept its identity across a flex swap). That reading is superseded, and it
    matters here because it inverts what a row means.

    `which == 'flex'` rows are therefore *not* the normal case:

      - a `tmp_uid` change with an unchanged `flex_fp` is the interesting one
        -- the TMP118 is the stronger of the two flex identifiers, so the
        pair disagreeing is a sign the record is wrong, not that hardware
        moved;
      - a `flex_fp` change means the row has stopped describing the unit it
        is keyed under, and `Devkit._set_identifier` logs an error saying
        which device id the new flex actually names.

    Nothing here is ever updated or deleted. The history of what was in a
    unit is the point of the table.
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


# --- end-of-line test results ------------------------------------------------

#: `eol_results.schema` values this code knows how to read. A result written
#: by a newer tool with a shape this server does not understand is refused at
#: the endpoint rather than stored half-read.
EOL_SCHEMA = 1

#: Check codes the gate is defined over, in the order an operator runs them.
#: A result missing any of these is incomplete, not passing: "we did not run
#: the known-load step" and "the known-load step passed" must never collapse
#: into the same stored row.
EOL_CHECKS = ("identity", "mags", "imu", "temp", "usb", "rate", "known_load")

#: `eol_results.tool` of a row the SERVER wrote from a calibration job's test
#: split (WI-546), as opposed to one `axio-devkit eol` wrote at the bench.
#: Such a row carries only the `known_load` check -- accuracy and the
#: sensing chain are all the recorded captures can establish; the six live
#: checks need the board -- so it is stored with `passed = False` (an
#: incomplete test is not a passing one) and the known-load verdict lives in
#: `checks["known_load"]["passed"]`. It can therefore never ship a unit on
#: its own; `shippability()` names it as what it is.
AUTO_EOL_TOOL = "axio-server auto-eol"
AUTO_EOL_CHECKS = ("known_load",)


def is_automatic_eol(row) -> bool:
    return getattr(row, "tool", None) == AUTO_EOL_TOOL


class EolResult(Base):
    """One end-of-line test run against one physical unit. Append-only.

    This table is the whole representation of the acceptance criterion *"a
    unit cannot be marked shippable without a passing EOL test on file"*. It
    is a separate table rather than a set of columns on `devkits`, for one
    reason worth stating plainly: **columns are PATCHable.** A
    `devkits.shippable` boolean, however carefully documented, is one request
    away from being set by hand by whoever most wants the unit to ship. A
    result row is evidence, and shippability is derived from the evidence at
    read time by `shippability()` — there is no writable flag anywhere.

    Consequences of that, all deliberate:

      - there is no PATCH and no DELETE for these rows. A unit that failed
        and was reworked gets a **new** result, and the failure stays on file.
      - `passed` is written by the test tool, not by an operator, and it is
        only half the story: `shippability()` additionally requires the row's
        identifiers to still match the unit's current ones, so a board swap
        after a passing test un-ships the unit rather than coasting on an old
        PASS.
      - `simulated` results can never make anything shippable. They are
        stored anyway: a run against `SimulatedDevice` is how the fixture
        itself is tested, and refusing them would mean the exercised path and
        the real path are not the same path.

    Keyed on all three identifiers (`flex_fp`, `tmp_uid`, `mcu_uid`) and not
    on the device id alone, because the device id is derived from `flex_fp`
    and therefore proves only one of the two boards. A result that cannot
    name the base board it ran against cannot be used to ship one.

    The UNIDENTIFIED sentinel is refused as `flex_fp` here, unlike on
    `devkits`. On `devkits`, "we saw this unit and its fingerprint did not
    form" is a fact worth storing about a board we hold. An EOL *result* is a
    statement about a specific unit, and a result that cannot be attributed
    to one is not a weaker record — it is a record of nothing.
    """

    __tablename__ = "eol_results"

    id = Column(Integer, primary_key=True, autoincrement=True)

    #: `18.<flex_fp>`. Not a ForeignKey to `devkits`, matching how the rest of
    #: this schema treats device ids: a unit is tested on the bench before
    #: anything has created a record for it, and a test result that could not
    #: be stored until the paperwork existed would simply not be stored.
    device_axf_id = Column(String, nullable=False, index=True)

    # --- the three identifiers this result is about ---
    flex_fp = Column(String, nullable=False, index=True)
    tmp_uid = Column(String, nullable=True, index=True)
    mcu_uid = Column(String, nullable=True, index=True)

    #: The verdict, as the tool computed it. Every check in `EOL_CHECKS` has
    #: to have passed for this to be True.
    passed = Column(Boolean, nullable=False, index=True)

    #: True when the run was against `SimulatedDevice` rather than hardware.
    #: Never shippable; see the class docstring.
    simulated = Column(Boolean, nullable=False, default=False)

    #: Per-check detail, `{code: {"passed": bool, "detail": str, ...}}`, as
    #: the tool emitted it. Stored whole rather than exploded into columns:
    #: the checks will change as the fixture grows, and a JSON blob that
    #: round-trips is worth more than a schema migration per check.
    checks = Column(JSON, nullable=True)

    #: Blockers the tool declared — checks it could not really perform. The
    #: known-load step is stubbed to fail closed until the fixture exists,
    #: and that is recorded here rather than buried inside `checks`.
    blockers = Column(JSON, nullable=True)

    # --- what was running when it was tested ---
    firmware_version = Column(String, nullable=True)
    model_version = Column(String, nullable=True)
    #: OPERATOR-ENTERED, same caveat as `Devkit.board_rev`.
    board_rev = Column(String, nullable=True)

    measured_rate_hz = Column(Float, nullable=True)
    rated_rate_hz = Column(Float, nullable=True)

    # --- provenance ---
    operator = Column(String, nullable=True)
    station = Column(String, nullable=True)
    tool = Column(String, nullable=True)            # e.g. 'axio-devkit eol'
    tool_version = Column(String, nullable=True)
    #: Named `result_schema` on the row as well as on the model: `schema` is
    #: a keyword in enough tooling (and an attribute on pydantic's BaseModel)
    #: that a column spelled that way is a standing trip hazard. It is still
    #: `schema` on the wire, where it matches the diagnostics bundle.
    result_schema = Column(Integer, nullable=False, default=EOL_SCHEMA)

    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    #: When the server stored it — the only timestamp here the server can
    #: vouch for, since the other two come off the test host's clock.
    recorded_at = Column(DateTime(timezone=True), nullable=False,
                         default=current_time)

    notes = Column(Text, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "device_axf_id": self.device_axf_id,
            "flex_fp": self.flex_fp,
            "tmp_uid": self.tmp_uid,
            "mcu_uid": self.mcu_uid,
            "passed": self.passed,
            "simulated": self.simulated,
            "checks": self.checks,
            "blockers": self.blockers,
            "firmware_version": self.firmware_version,
            "model_version": self.model_version,
            "board_rev": self.board_rev,
            "measured_rate_hz": self.measured_rate_hz,
            "rated_rate_hz": self.rated_rate_hz,
            "operator": self.operator,
            "station": self.station,
            "tool": self.tool,
            "tool_version": self.tool_version,
            "schema": self.result_schema,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "recorded_at": self.recorded_at,
            "notes": self.notes,
        }


Index("ix_eol_results_device_time",
      EolResult.device_axf_id, EolResult.completed_at)


def _eol_when(row: "EolResult") -> str:
    when = row.completed_at or row.recorded_at
    return f" on {when.strftime('%Y-%m-%d')}" if when else ""


def _identifier_mismatch(row: "EolResult",
                         devkit: Optional["Devkit"]) -> List[str]:
    """Which of the result's identifiers disagree with the unit's current ones.

    An identifier the *result* does not carry cannot disagree — an older tool
    that never read the TMP118 must not un-ship a unit. An identifier the
    *record* does not carry cannot disagree either: a `devkits` row that has
    never been told its `mcu_uid` has no claim to contradict. Only two
    present, differing values are a mismatch. Case is not a difference: the
    record spells uids in uppercase (`format_tmp_uid` / `format_mcu_uid`)
    and a hand-uploaded result may not.
    """
    if devkit is None:
        return []
    out = []
    for field in ("flex_fp", "tmp_uid", "mcu_uid"):
        tested = getattr(row, field)
        current = getattr(devkit, field)
        if tested and current and \
                tested.strip().lower() != current.strip().lower():
            out.append(field)
    return out


def shippability(db: Session, device_axf_id: str,
                 devkit: Optional["Devkit"] = None) -> dict:
    """Is this unit shippable, and if not, why not. Derived, never stored.

    Returns `{"shippable": bool, "reason": str, "eol_result_id": int|None,
    "passed_at": datetime|None}`.

    A unit is shippable when there is an `eol_results` row for it that

      1. passed,
      2. was not simulated, and
      3. still matches the unit's current identifiers.

    (3) is the clause that earns this being a function rather than a column.
    A unit that passed EOL and then had its base board swapped has a passing
    test on file for hardware it no longer contains; a stored flag would
    still read True, and the swap is recorded in `devkit_assignment_history`
    precisely because nobody would otherwise notice. Deriving it means the
    swap un-ships the unit the moment it is recorded, with no second write
    for anyone to remember to make.

    `reason` is always populated, success included, because "why is this not
    shippable" is the question this gets asked and an empty string is not an
    answer.
    """
    if not is_devkit_id(device_axf_id):
        return {"shippable": False, "eol_result_id": None, "passed_at": None,
                "reason": f"{device_axf_id} is not a dev-kit device id "
                          f"(type {DEVKIT_TYPE_ID})."}
    if flex_fp_from_device_id(device_axf_id) is None:
        return {"shippable": False, "eol_result_id": None, "passed_at": None,
                "reason": f"{device_axf_id} is the UNIDENTIFIED flex "
                          f"sentinel, not a device. A unit with no "
                          f"fingerprint can never be shipped: no model can "
                          f"bind to it."}

    rows = (db.query(EolResult)
            .filter(EolResult.device_axf_id == device_axf_id)
            .order_by(EolResult.completed_at.desc().nullslast(),
                      EolResult.id.desc())
            .all())
    if not rows:
        return {"shippable": False, "eol_result_id": None, "passed_at": None,
                "reason": f"No end-of-line test is on file for "
                          f"{device_axf_id}. A unit cannot be shipped "
                          f"without one."}

    # An automatic row is excluded even if it were ever stored as passed: it
    # cannot have run the live checks, so it is never the evidence a unit
    # ships on (see AUTO_EOL_TOOL).
    passing = [r for r in rows
               if r.passed and not r.simulated and not is_automatic_eol(r)]
    if not passing:
        if any(r.passed and r.simulated for r in rows):
            return {
                "shippable": False, "eol_result_id": None, "passed_at": None,
                "reason": f"The only passing end-of-line results on file for "
                          f"{device_axf_id} were run against the simulator, "
                          f"not against hardware. A simulated pass says "
                          f"nothing about a physical unit."}
        bench = [r for r in rows if not is_automatic_eol(r)]
        if not bench:
            # Only the server's automatic known-load evidence is on file. Say
            # what it found and what is still missing, rather than calling a
            # row that could not run six of seven checks a failed test.
            auto = rows[0]
            kl = (auto.checks or {}).get("known_load") or {}
            verdict = "passed" if kl.get("passed") else "did not pass"
            return {
                "shippable": False, "eol_result_id": None, "passed_at": None,
                "reason": f"Only automatic end-of-line evidence is on file for "
                          f"{device_axf_id}: the known-load check from its "
                          f"calibration test split (#{auto.id}) {verdict}. "
                          f"The live checks ({', '.join(c for c in EOL_CHECKS if c not in AUTO_EOL_CHECKS)}) "
                          f"need the unit on the bench (axio-devkit eol)."}
        latest = bench[0]
        failed = [c for c, v in sorted((latest.checks or {}).items())
                  if isinstance(v, dict) and not v.get("passed")]
        detail = f" Failing checks: {', '.join(failed)}." if failed else ""
        return {"shippable": False, "eol_result_id": None, "passed_at": None,
                "reason": f"The most recent end-of-line test for "
                          f"{device_axf_id} did not pass.{detail}"}

    if devkit is None:
        devkit = (db.query(Devkit)
                  .filter(Devkit.device_axf_id == device_axf_id).first())

    for row in passing:
        if not _identifier_mismatch(row, devkit):
            if devkit is None:
                # The result carries all three identifiers, so it is still
                # evidence about a specific unit -- but there is no record to
                # contradict it either, which is a different and weaker
                # thing. Say which of the two this is.
                return {"shippable": True, "eol_result_id": row.id,
                        "passed_at": row.completed_at or row.recorded_at,
                        "reason": f"End-of-line test #{row.id} passed"
                                  f"{_eol_when(row)}. NOTE: no devkits record "
                                  f"exists for this unit, so nothing is on "
                                  f"file to confirm the boards tested are "
                                  f"still the boards in it."}
            return {"shippable": True, "eol_result_id": row.id,
                    "passed_at": row.completed_at or row.recorded_at,
                    "reason": f"End-of-line test #{row.id} passed"
                              f"{_eol_when(row)}, against the boards this "
                              f"unit still contains."}

    row = passing[0]
    changed = ", ".join(_identifier_mismatch(row, devkit))
    return {"shippable": False, "eol_result_id": None, "passed_at": None,
            "reason": f"{device_axf_id} has a passing end-of-line test "
                      f"(#{row.id}{_eol_when(row)}), but the unit's "
                      f"{changed} no longer matches what was tested. A board "
                      f"has been swapped since; the unit needs a fresh EOL "
                      f"run."}



# --- request/response shapes -------------------------------------------------

class DevkitRequest(BaseModel):
    device_axf_id: str
    device_type_id: str = "18"
    flex_fp: Optional[str] = None
    tmp_uid: Optional[str] = None
    mcu_uid: Optional[str] = None
    #: Operator-entered; the device reports 0.0.0 for every board.
    board_rev: Optional[str] = None
    firmware_version: Optional[str] = None
    model_version: Optional[str] = None
    calibration_date: Optional[datetime] = None
    operator: Optional[str] = None
    magnet_lot: Optional[str] = None
    #: The five HAL3304 die strings, bus 1..5 in order.
    mag_ids: Optional[List[str]] = None
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
    calibration_date: Optional[datetime] = None
    operator: Optional[str] = None
    magnet_lot: Optional[str] = None
    mag_ids: Optional[List[str]] = None
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
    calibration_date: Optional[datetime] = None
    operator: Optional[str] = None
    magnet_lot: Optional[str] = None
    mag_ids: Optional[List[str]] = None
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
    #: Every end-of-line test on file for this unit, newest first.
    eol_results: List["EolResultResponse"] = []
    #: DERIVED from `eol_results` on every read; there is no stored flag.
    #: See `shippability()` for the rule and `EolResult` for why it is a
    #: derivation rather than a column.
    shippable: bool = False
    shippable_reason: str = ""
    #: Plain-language summary of everything above, for a support reply.
    summary: str


class EolResultRequest(BaseModel):
    """The JSON `axio-devkit eol` writes, as the POST body.

    Deliberately the tool's own output shape rather than a hand-written
    server DTO: the operator's workflow is `axio-devkit eol --json
    result.json` then upload that file, and a body that needs reshaping in
    between is a body somebody will reshape by hand.

    `passed` is carried rather than recomputed because the tool is what ran
    the checks. The server does not take it on trust either — the endpoint
    cross-checks it against `checks`, since a payload claiming a pass with a
    failing check in it is the one payload that must not be stored quietly.
    """
    model_config = ConfigDict(populate_by_name=True)

    device_axf_id: Optional[str] = None
    flex_fp: Optional[str] = None
    tmp_uid: Optional[str] = None
    mcu_uid: Optional[str] = None

    passed: bool
    simulated: bool = False
    checks: Optional[dict] = None
    blockers: Optional[List[str]] = None

    firmware_version: Optional[str] = None
    model_version: Optional[str] = None
    board_rev: Optional[str] = None

    measured_rate_hz: Optional[float] = None
    rated_rate_hz: Optional[float] = None

    operator: Optional[str] = None
    station: Optional[str] = None
    tool: Optional[str] = None
    tool_version: Optional[str] = None
    #: Spelled `schema` on the wire, matching the diagnostics bundle. The
    #: Python attribute is renamed because `schema` shadows a BaseModel
    #: attribute.
    result_schema: int = Field(
        default=EOL_SCHEMA,
        validation_alias=AliasChoices("schema", "result_schema"),
        serialization_alias="schema")

    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    notes: Optional[str] = None


class EolResultResponse(BaseModel):
    """One stored EOL result."""
    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    id: int
    device_axf_id: str
    flex_fp: Optional[str] = None
    tmp_uid: Optional[str] = None
    mcu_uid: Optional[str] = None
    passed: bool
    simulated: bool = False
    checks: Optional[dict] = None
    blockers: Optional[List[str]] = None
    firmware_version: Optional[str] = None
    model_version: Optional[str] = None
    board_rev: Optional[str] = None
    measured_rate_hz: Optional[float] = None
    rated_rate_hz: Optional[float] = None
    operator: Optional[str] = None
    station: Optional[str] = None
    tool: Optional[str] = None
    tool_version: Optional[str] = None
    result_schema: int = Field(
        default=EOL_SCHEMA,
        validation_alias=AliasChoices("result_schema", "schema"),
        serialization_alias="schema")
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    recorded_at: datetime
    notes: Optional[str] = None


class ShippabilityResponse(BaseModel):
    """Whether a unit may ship, and the evidence behind the answer.

    `shippable` is computed by `shippability()` on every read. There is no
    stored flag to disagree with, which is the point.
    """
    device_axf_id: str
    shippable: bool
    reason: str
    eol_result_id: Optional[int] = None
    passed_at: Optional[datetime] = None


# `DevkitRecordResponse.eol_results` names `EolResultResponse`, which is
# defined below it (the record response sits with the other devkit shapes,
# the EOL shapes with the EOL model). Resolve the forward reference now
# rather than leaving it to the first request to fail on.
DevkitRecordResponse.model_rebuild()
