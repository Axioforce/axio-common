"""The six-axis load cell's end-of-line record, its shippability and its certificate (#1423).

The load cell's counterpart of the dev kit's `EolResult` / `shippability()`
(`models/devkit.py`), keyed by the unit's **serial** (`loadcell_units`), the
training **job** whose model it carries and the **CRC of the pack** installed.

  - `LoadCellEolResult` (`loadcell_eol_results`): one end-of-line run,
    append-only, exactly as the SDK's `axio-loadcell-internal eol` emits it
    (or the server's automatic row, `LOADCELL_AUTO_EOL_TOOL`). There is no
    PATCH and no DELETE: a failure stays on file beside the pass that follows
    the rework.
  - `loadcell_shippability(db, serial)`: **derived on every read**, never a
    stored flag. A unit is shippable when all three hold:
      1. it is **calibrated**: typed, with a pack recorded
         (`loadcell_calibrations`, whose newest row is what it carries);
      2. an **on-board EOL pass** is on file (not simulated, not the server's
         automatic row, which cannot run the live checks) **for the same pack
         CRC** as that newest calibration;
      3. **no calibration was recorded after that pass**: a re-calibration,
         even with the same pack, needs a new EOL.
  - `LoadCellCertificate` and `loadcell_certificate(db, serial)`: the data of
    the calibration certificate, assembled from those records (the server
    renders it). Nothing on it is a new fact.

The pass/fail limits of the check are **provisional** (placeholders, not yet
set from real units, SDK `accuracy.PROVISIONAL_THRESHOLDS`); every row records
the limits it was held to and whether they were provisional, and the
certificate carries that as a banner.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON,
    String, Text,
)
from sqlalchemy.orm import Session

from axio_common.database import Base
from axio_common.models.loadcell import (
    LOADCELL_TYPE_NAMES, LoadCellCalibration, LoadCellUnit, format_serial,
)
from axio_common.utils.model_utils import current_time

#: The result document's shape (SDK `eol.EOL_SCHEMA`). A newer one is refused
#: rather than stored half-read.
LOADCELL_EOL_SCHEMA = 1

#: Every check a claimed pass must carry, passed (SDK `eol.CHECKS`).
LOADCELL_EOL_CHECKS = ("identity", "model", "selftest", "stream", "tare",
                       "accuracy")

#: The bench tool and the server's automatic row.
LOADCELL_EOL_TOOL = "axio-loadcell-internal eol"
LOADCELL_AUTO_EOL_TOOL = "axio-server loadcell auto-eol"
#: The checks an automatic row can form (from recorded captures only).
LOADCELL_AUTO_EOL_CHECKS = ("accuracy",)

#: The six outputs, in pack order, and their units.
LOADCELL_OUTPUTS = ("Fx", "Fy", "Fz", "Tx", "Ty", "Tz")
LOADCELL_OUTPUT_UNITS = ("N", "N", "N", "N m", "N m", "N m")


def _aware(t: Optional[datetime]) -> Optional[datetime]:
    if t is not None and t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t


class LoadCellEolResult(Base):
    """One end-of-line run against one load cell. Append-only.

    `passed` is the tool's verdict over every check in `LOADCELL_EOL_CHECKS`;
    the server refuses a claimed pass with a check missing or failed, and an
    automatic row is never a pass. `figures` (the held-out accuracy figures,
    SDK `accuracy.figures`) and `thresholds` (the limits used) are stored whole
    so a later, real specification can be applied to old rows.
    """
    __tablename__ = "loadcell_eol_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: 8 lowercase hex, the unit's key. A unit is registered before its EOL.
    serial = Column(String, ForeignKey("loadcell_units.serial"),
                    nullable=False, index=True)
    #: 24 lowercase hex as IDENTIFY reported it at the test (None for an
    #: automatic row: no board was attached).
    mcu_uid = Column(String, nullable=True)
    device_type_id = Column(String, nullable=True)
    device_axf_id = Column(String, nullable=True)
    #: The training job whose model the unit was checked against (not a FK:
    #: a job may be deleted, the record of a shipped unit may not).
    job_id = Column(String, nullable=True, index=True)
    #: The job's packed YYYYMMDDHHMMSS integer UID. Never a date.
    job_timestamp = Column(BigInteger, nullable=True)
    #: The pack the board carried (IDENTIFY model_version / model_id), 8 hex.
    #: None for an automatic row, which scores the job's model files.
    model_version = Column(String, nullable=True)
    model_crc32 = Column(String, nullable=True, index=True)

    passed = Column(Boolean, nullable=False, index=True)
    simulated = Column(Boolean, nullable=False, default=False)
    checks = Column(JSON, nullable=True)
    figures = Column(JSON, nullable=True)
    thresholds = Column(JSON, nullable=True)
    provisional = Column(Boolean, nullable=False, default=True)
    blockers = Column(JSON, nullable=True)

    firmware_version = Column(String, nullable=True)
    #: IDENTIFY build_flavor: 0 development image, 1 release.
    build_flavor = Column(Integer, nullable=True)
    operator = Column(String, nullable=True)
    station = Column(String, nullable=True)
    tool = Column(String, nullable=True)
    tool_version = Column(String, nullable=True)
    result_schema = Column(Integer, nullable=False, default=LOADCELL_EOL_SCHEMA)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    #: When the server stored it: the clock shippability compares with the
    #: calibrations' `recorded_at`.
    recorded_at = Column(DateTime(timezone=True), nullable=False,
                         default=current_time)
    notes = Column(Text, nullable=True)


Index("ix_loadcell_eol_results_serial_time",
      LoadCellEolResult.serial, LoadCellEolResult.recorded_at)


def is_automatic_loadcell_eol(row) -> bool:
    return getattr(row, "tool", None) == LOADCELL_AUTO_EOL_TOOL


def latest_calibration(db: Session, serial: str) -> Optional[LoadCellCalibration]:
    """The calibration the unit carries: the newest recorded."""
    return (db.query(LoadCellCalibration)
            .filter(LoadCellCalibration.serial == serial)
            .order_by(LoadCellCalibration.recorded_at.desc(),
                      LoadCellCalibration.id.desc())
            .first())


def _no(reason: str, **extra) -> dict:
    out = {"shippable": False, "reason": reason, "eol_result_id": None,
           "passed_at": None, "calibration_id": None, "model_crc32": None}
    out.update(extra)
    return out


def loadcell_shippability(db: Session, serial) -> dict:
    """May this unit ship, and why (not). Derived, never stored.

    `{"shippable", "reason", "eol_result_id", "passed_at", "calibration_id",
    "model_crc32"}`; `reason` is always a sentence, success included. The
    three conditions are the module docstring's.
    """
    try:
        serial = format_serial(serial)
    except ValueError as e:
        return _no(str(e))
    unit = db.query(LoadCellUnit).filter(LoadCellUnit.serial == serial).first()
    if unit is None:
        return _no(f"Load cell {serial} is not registered.")
    cal = latest_calibration(db, serial)
    if not unit.device_type_id or not unit.model_crc32 or cal is None:
        return _no(f"Load cell {serial} is not calibrated: no model pack is "
                   f"recorded as installed on it.")
    crc = cal.model_crc32
    extra = {"calibration_id": cal.id, "model_crc32": crc}
    rows = (db.query(LoadCellEolResult)
            .filter(LoadCellEolResult.serial == serial)
            .order_by(LoadCellEolResult.recorded_at.desc(),
                      LoadCellEolResult.id.desc())
            .all())
    bench = [r for r in rows if not is_automatic_loadcell_eol(r)]
    real = [r for r in bench if not r.simulated]
    passes = [r for r in real if r.passed]
    cal_at = _aware(cal.recorded_at)
    good = [r for r in passes if r.model_crc32 == crc
            and _aware(r.recorded_at) >= cal_at]
    if good:
        r = good[0]
        return {"shippable": True, "eol_result_id": r.id,
                "passed_at": r.completed_at or r.recorded_at,
                "calibration_id": cal.id, "model_crc32": crc,
                "reason": (f"End-of-line #{r.id} passed on "
                           f"{_aware(r.recorded_at):%Y-%m-%d} with pack {crc}, "
                           f"the pack of its latest calibration (#{cal.id})"
                           f"{', against provisional limits' if r.provisional else ''}.")}
    if not rows:
        return _no(f"No end-of-line check is on file for load cell {serial}.",
                   **extra)
    if not bench:
        auto = rows[0]
        acc = (auto.checks or {}).get("accuracy") or {}
        verdict = "passed" if acc.get("passed") else "did not pass"
        return _no(f"Only the server's automatic end-of-line evidence is on "
                   f"file for load cell {serial}: the held-out accuracy of "
                   f"job {auto.job_id} (#{auto.id}) {verdict}. The live checks "
                   f"need the unit on the bench (axio-loadcell-internal eol).",
                   **extra)
    if not real:
        return _no(f"The only end-of-line checks on file for load cell {serial} "
                   f"ran against the simulator.", **extra)
    same = [r for r in passes if r.model_crc32 == crc]
    if same:
        r = same[0]
        return _no(f"Load cell {serial} was calibrated again (#{cal.id}, "
                   f"{cal_at:%Y-%m-%d %H:%M} UTC) after its end-of-line pass "
                   f"#{r.id}; it needs a new end-of-line check.", **extra)
    if passes:
        r = passes[0]
        return _no(f"The end-of-line pass on file (#{r.id}) was for pack "
                   f"{r.model_crc32}; load cell {serial} now carries pack {crc} "
                   f"(calibration #{cal.id}). It needs an end-of-line check "
                   f"with that pack.", **extra)
    latest = real[0]
    failed = sorted(c for c, v in (latest.checks or {}).items()
                    if isinstance(v, dict) and not v.get("passed"))
    detail = f" Failing checks: {', '.join(failed)}." if failed else ""
    return _no(f"The most recent end-of-line check for load cell {serial} "
               f"(#{latest.id}) did not pass.{detail}", **extra)


# --- API shapes --------------------------------------------------------------

class LoadCellEolRequest(BaseModel):
    """The JSON `axio-loadcell-internal eol` writes, as the POST body of
    `POST /loadcell/units/{serial}/eol`, unmodified. `passed` is the tool's;
    the server cross-checks it against `checks`."""
    model_config = ConfigDict(populate_by_name=True, protected_namespaces=())

    serial: Optional[Union[str, int]] = None
    mcu_uid: Optional[str] = None
    device_type_id: Optional[str] = None
    device_axf_id: Optional[str] = None
    job_id: Optional[str] = None
    job_timestamp: Optional[int] = None
    model_version: Optional[Union[str, int]] = None
    model_crc32: Optional[Union[str, int]] = None
    passed: bool
    simulated: bool = False
    checks: Optional[Dict[str, Any]] = None
    figures: Optional[Dict[str, Any]] = None
    thresholds: Optional[Dict[str, Any]] = None
    provisional: bool = True
    blockers: Optional[List[str]] = None
    firmware_version: Optional[str] = None
    build_flavor: Optional[int] = None
    operator: Optional[str] = None
    station: Optional[str] = None
    tool: Optional[str] = None
    tool_version: Optional[str] = None
    result_schema: int = Field(
        default=LOADCELL_EOL_SCHEMA,
        validation_alias=AliasChoices("schema", "result_schema"),
        serialization_alias="schema")
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    notes: Optional[str] = None
    host: Optional[Dict[str, Any]] = None


class LoadCellEolResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True, from_attributes=True,
                              protected_namespaces=())

    id: int
    serial: str
    mcu_uid: Optional[str] = None
    device_type_id: Optional[str] = None
    device_axf_id: Optional[str] = None
    job_id: Optional[str] = None
    job_timestamp: Optional[int] = None
    model_version: Optional[str] = None
    model_crc32: Optional[str] = None
    passed: bool
    simulated: bool = False
    checks: Optional[Dict[str, Any]] = None
    figures: Optional[Dict[str, Any]] = None
    thresholds: Optional[Dict[str, Any]] = None
    provisional: bool = True
    blockers: Optional[List[str]] = None
    firmware_version: Optional[str] = None
    build_flavor: Optional[int] = None
    operator: Optional[str] = None
    station: Optional[str] = None
    tool: Optional[str] = None
    tool_version: Optional[str] = None
    result_schema: int = Field(
        default=LOADCELL_EOL_SCHEMA,
        validation_alias=AliasChoices("result_schema", "schema"),
        serialization_alias="schema")
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    recorded_at: datetime
    notes: Optional[str] = None


class LoadCellShippabilityResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    serial: str
    shippable: bool
    reason: str
    eol_result_id: Optional[int] = None
    passed_at: Optional[datetime] = None
    calibration_id: Optional[int] = None
    model_crc32: Optional[str] = None


# --- the certificate's data --------------------------------------------------

class LoadCellCertificateAxis(BaseModel):
    """One output's figures as the certificate prints them."""
    output: str                     #: "Fx"
    unit: str                       #: "N" or "N m"
    rated: Optional[float] = None   #: full scale; None when unknown (0)
    mae: Optional[float] = None
    max_abs: Optional[float] = None
    rmse: Optional[float] = None
    mae_pct_fs: Optional[float] = None
    max_pct_fs: Optional[float] = None
    rmse_pct_fs: Optional[float] = None
    repeatability: Optional[float] = None


class LoadCellCertificate(BaseModel):
    """Everything a load cell's calibration certificate says, from the
    records. `shippable` False means the server refuses to issue it (a draft
    only); `provisional` True prints the banner."""
    model_config = ConfigDict(protected_namespaces=())

    serial: str
    mcu_uid: str
    device_type_id: Optional[str] = None
    type_name: Optional[str] = None
    device_axf_id: Optional[str] = None
    model_version: Optional[str] = None
    model_crc32: Optional[str] = None
    calibration_id: Optional[int] = None
    calibration_date: Optional[datetime] = None
    job_id: Optional[str] = None
    job_timestamp: Optional[int] = None
    board_rev: Optional[str] = None
    pad_lot: Optional[str] = None
    shippable: bool
    shippable_reason: str
    eol_result_id: Optional[int] = None
    eol_passed: Optional[bool] = None
    eol_simulated: Optional[bool] = None
    eol_completed_at: Optional[datetime] = None
    eol_operator: Optional[str] = None
    eol_station: Optional[str] = None
    eol_firmware_version: Optional[str] = None
    eol_checks: Dict[str, bool] = {}
    eol_blockers: List[str] = []
    provisional: bool = True
    thresholds: Optional[Dict[str, Any]] = None
    axes: List[LoadCellCertificateAxis] = []
    by_activity: Dict[str, Any] = {}
    scored_rows: Optional[int] = None
    repeatability_measured: bool = False


def _axes(figures: Optional[dict]) -> List[LoadCellCertificateAxis]:
    if not isinstance(figures, dict) or not isinstance(figures.get("all"), dict):
        return []
    a = figures["all"]
    rated = list(figures.get("rated") or [0.0] * 6)
    rep = figures.get("repeatability") or {}
    sd = rep.get("sd") if rep.get("measured") else None

    def at(key, k):
        v = a.get(key)
        return v[k] if isinstance(v, list) and len(v) > k else None

    return [LoadCellCertificateAxis(
        output=name, unit=LOADCELL_OUTPUT_UNITS[k],
        rated=(float(rated[k]) if k < len(rated) and rated[k] else None),
        mae=at("mae", k), max_abs=at("max_abs", k), rmse=at("rmse", k),
        mae_pct_fs=at("mae_pct_fs", k), max_pct_fs=at("max_pct_fs", k),
        rmse_pct_fs=at("rmse_pct_fs", k),
        repeatability=(sd[k] if isinstance(sd, list) and len(sd) > k else None))
        for k, name in enumerate(LOADCELL_OUTPUTS)]


def loadcell_certificate(db: Session, serial) -> Optional[LoadCellCertificate]:
    """The certificate's data for a registered unit, or None when it is not
    registered. The EOL shown is the passing row shippability names; for an
    unshippable unit (a draft), the newest bench row, else the newest row."""
    serial = format_serial(serial)
    unit = db.query(LoadCellUnit).filter(LoadCellUnit.serial == serial).first()
    if unit is None:
        return None
    ship = loadcell_shippability(db, serial)
    cal = latest_calibration(db, serial)
    eol = None
    if ship["eol_result_id"] is not None:
        eol = db.get(LoadCellEolResult, ship["eol_result_id"])
    else:
        rows = (db.query(LoadCellEolResult)
                .filter(LoadCellEolResult.serial == serial)
                .order_by(LoadCellEolResult.recorded_at.desc(),
                          LoadCellEolResult.id.desc()).all())
        bench = [r for r in rows if not is_automatic_loadcell_eol(r)]
        eol = (bench or rows or [None])[0]
    figures = eol.figures if eol is not None else None
    checks = {}
    if eol is not None and isinstance(eol.checks, dict):
        checks = {c: bool(isinstance(v, dict) and v.get("passed"))
                  for c, v in eol.checks.items()}
    return LoadCellCertificate(
        serial=serial, mcu_uid=unit.mcu_uid,
        device_type_id=unit.device_type_id,
        type_name=LOADCELL_TYPE_NAMES.get(unit.device_type_id or ""),
        device_axf_id=unit.device_axf_id,
        model_version=(cal.model_version if cal else unit.model_version),
        model_crc32=(cal.model_crc32 if cal else unit.model_crc32),
        calibration_id=cal.id if cal else None,
        calibration_date=(cal.calibration_date if cal else unit.calibration_date),
        job_id=(eol.job_id if eol is not None and eol.job_id
                else (cal.job_id if cal else None)),
        job_timestamp=(eol.job_timestamp if eol is not None and eol.job_timestamp
                       else (cal.job_timestamp if cal else None)),
        board_rev=unit.board_rev, pad_lot=unit.pad_lot,
        shippable=bool(ship["shippable"]), shippable_reason=ship["reason"],
        eol_result_id=eol.id if eol is not None else None,
        eol_passed=bool(eol.passed) if eol is not None else None,
        eol_simulated=bool(eol.simulated) if eol is not None else None,
        eol_completed_at=(eol.completed_at or eol.recorded_at) if eol is not None else None,
        eol_operator=eol.operator if eol is not None else None,
        eol_station=eol.station if eol is not None else None,
        eol_firmware_version=eol.firmware_version if eol is not None else None,
        eol_checks=checks,
        eol_blockers=list(eol.blockers or []) if eol is not None else [],
        provisional=bool(eol.provisional) if eol is not None else True,
        thresholds=eol.thresholds if eol is not None else None,
        axes=_axes(figures),
        by_activity=(figures.get("by_activity") or {}) if isinstance(figures, dict) else {},
        scored_rows=((figures.get("rows") or {}).get("scored")
                     if isinstance(figures, dict) else None),
        repeatability_measured=bool(((figures or {}).get("repeatability") or {})
                                    .get("measured")) if isinstance(figures, dict) else False)


__all__ = [
    "LOADCELL_EOL_SCHEMA", "LOADCELL_EOL_CHECKS", "LOADCELL_EOL_TOOL",
    "LOADCELL_AUTO_EOL_TOOL", "LOADCELL_AUTO_EOL_CHECKS", "LOADCELL_OUTPUTS",
    "LOADCELL_OUTPUT_UNITS",
    "LoadCellEolResult", "is_automatic_loadcell_eol", "latest_calibration",
    "loadcell_shippability", "LoadCellEolRequest", "LoadCellEolResponse",
    "LoadCellShippabilityResponse", "LoadCellCertificateAxis",
    "LoadCellCertificate", "loadcell_certificate",
]
