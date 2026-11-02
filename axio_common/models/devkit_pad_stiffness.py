"""Dev-kit pad stiffness: one row per measurement of a flex's pad (Dyno #1301).

A kit's pad stiffness used to be knowable only by going back through its
calibration data. axio-server now measures it from the unit's calibration
cylinder captures (the Neuralizer `stiffness_tr` method, vendored there) and
records every measurement here, newest last, so a re-measurement never
erases the one before it. The latest one is copied onto the `devkits` row
(`pad_*` columns) so a lookup is one row read.

Keyed by the device id, which for a dev kit IS the flex (`18.<flex_fp>`):
the pad is part of the flex, so its stiffness follows the flex exactly as the
calibration model does.

A measurement is either `measured` (E, Shore A equivalent, k from one tip)
or `not_measured` with a `reason` -- a kit whose calibration has no usable
cylinder captures (only box or BUS-program captures) gets a reason, never a
guessed value. `source` says who measured: `auto` (the server's sweep),
`backfill` (the one-off script); `manual` is reserved for a durometer
reading entered by hand.

Pads are described by measured stiffness only. Never record the pad
material, brand or grade here.
"""
from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel
from sqlalchemy import (
    BigInteger, Column, DateTime, Float, Index, Integer, JSON, String, Text,
)
from sqlalchemy.orm import Session

from axio_common.database import Base
from axio_common.logger import logger
from axio_common.utils.model_utils import current_time

PAD_STIFFNESS_MEASURED = "measured"
PAD_STIFFNESS_NOT_MEASURED = "not_measured"
PAD_STIFFNESS_STATUSES = (PAD_STIFFNESS_MEASURED, PAD_STIFFNESS_NOT_MEASURED)

PAD_STIFFNESS_SOURCE_AUTO = "auto"
PAD_STIFFNESS_SOURCE_BACKFILL = "backfill"
#: Reserved: a durometer reading entered by hand. Nothing writes it yet.
PAD_STIFFNESS_SOURCE_MANUAL = "manual"
PAD_STIFFNESS_SOURCES = (PAD_STIFFNESS_SOURCE_AUTO,
                         PAD_STIFFNESS_SOURCE_BACKFILL,
                         PAD_STIFFNESS_SOURCE_MANUAL)


class DevkitPadStiffness(Base):
    """One pad-stiffness measurement of one dev kit. Append-only."""

    __tablename__ = "devkit_pad_stiffness"

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: `18.<flex_fp>`. Not a ForeignKey, like the rest of the dev-kit
    #: tables: a measurement must never block deleting anything.
    device_axf_id = Column(String, nullable=False, index=True)
    flex_fp = Column(String, nullable=True)
    #: 'measured' | 'not_measured'.
    status = Column(String, nullable=False)
    #: Why there is no value (status 'not_measured'); NULL otherwise.
    reason = Column(Text, nullable=True)

    #: Cylinder diameter the reported value comes from (2, 4 or 6 mm).
    tip_mm = Column(Float, nullable=True)
    #: Median contact stiffness of the center presses, N/mm, and its IQR.
    k_n_per_mm = Column(Float, nullable=True)
    k_iqr = Column(JSON, nullable=True)
    #: Young's modulus, MPa (tip-corrected), and its IQR.
    e_mpa = Column(Float, nullable=True)
    e_iqr = Column(JSON, nullable=True)
    #: Shore A equivalent (Gent); an extrapolation below ~20A.
    shore_a = Column(Float, nullable=True)
    #: Uncertainty of E for the tip used, percent (5 for d2/d4, 10 for d6).
    uncertainty_pct = Column(Float, nullable=True)
    #: Center presses pooled for the reported tip.
    n_presses = Column(Integer, nullable=True)
    #: Every tip's pooled result: {"d4": {"k_N_per_mm", "k_iqr", "n",
    #: "E_MPa"?, ...}, "d8": {...}}. d8 carries k only.
    per_tip = Column(JSON, nullable=True)
    #: Bucket keys of the captures measured (sorted), and those skipped with
    #: why: [{"key", "reason"}].
    captures = Column(JSON, nullable=True)
    skipped_captures = Column(JSON, nullable=True)
    #: Hash of the capture set (keys + sizes) and the method commit; the
    #: sweep recomputes when it changes.
    capture_fingerprint = Column(String, nullable=True, index=True)
    #: The newest completed training job of the unit when this was measured
    #: (the packed YYYYMMDDHHMMSS job UID, an integer). NULL if none.
    job_timestamp = Column(BigInteger, nullable=True)

    method = Column(String, nullable=False)
    method_commit = Column(String, nullable=False)
    #: 'auto' | 'backfill' | 'manual'.
    source = Column(String, nullable=False)
    measured_at = Column(DateTime(timezone=True), nullable=False,
                         default=current_time)

    def to_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


Index("ix_devkit_pad_stiffness_device_time",
      DevkitPadStiffness.device_axf_id, DevkitPadStiffness.measured_at)


#: devkits columns that hold the latest measurement (see `Devkit`).
PAD_SUMMARY_FIELDS = (
    "pad_stiffness_status", "pad_stiffness_reason", "pad_shore_a",
    "pad_e_mpa", "pad_k_n_per_mm", "pad_stiffness_tip_mm",
    "pad_stiffness_uncertainty_pct", "pad_stiffness_n_presses",
    "pad_stiffness_measured_at",
)


def _num(v) -> Optional[float]:
    return None if v is None else float(v)


def record_pad_stiffness(db: Session, device_axf_id: str, result: dict, *,
                         method: str, method_commit: str, source: str,
                         captures: Optional[List[str]] = None,
                         skipped_captures: Optional[List[dict]] = None,
                         capture_fingerprint: Optional[str] = None,
                         job_timestamp: Optional[int] = None,
                         measured_at: Optional[datetime] = None,
                         ) -> Optional[DevkitPadStiffness]:
    """Append one measurement and make it the unit's summary.

    `result` is the method's `combine()` output: `E_MPa`, `shore_A`,
    `k_N_per_mm`, `k_iqr`, `E_iqr`, `tip_mm`, `n`, `uncertainty_pct`,
    `by_tip`, or `reason` when nothing could be measured. A result with a
    `reason`, or without `E_MPa`, is stored as not measured; nothing is
    guessed.

    Creates the `devkits` row when there is none (as `record_calibration`
    does). Refuses non-dev-kit ids and the UNIDENTIFIED sentinel (returns
    None). Does NOT commit; the caller owns the transaction.
    """
    from axio_common.models.devkit import (
        DEVKIT_TYPE_ID, Devkit, flex_fp_from_device_id, is_devkit_id,
    )
    if source not in PAD_STIFFNESS_SOURCES:
        raise ValueError(f"source must be one of {PAD_STIFFNESS_SOURCES}")
    if not is_devkit_id(device_axf_id):
        return None
    axf = f"{DEVKIT_TYPE_ID}.{device_axf_id.strip()[3:].lower()}"
    fp = flex_fp_from_device_id(axf)
    if fp is None:
        logger.warning(f"Refusing to record pad stiffness against {axf}: "
                       f"the UNIDENTIFIED flex sentinel is not a device.")
        return None

    measured = result.get("reason") is None and result.get("E_MPa") is not None
    when = measured_at or current_time()
    row = DevkitPadStiffness(
        device_axf_id=axf, flex_fp=fp,
        status=PAD_STIFFNESS_MEASURED if measured else
        PAD_STIFFNESS_NOT_MEASURED,
        reason=None if measured else (result.get("reason")
                                      or "no stiffness value formed"),
        tip_mm=_num(result.get("tip_mm")) if measured else None,
        k_n_per_mm=_num(result.get("k_N_per_mm")) if measured else None,
        k_iqr=result.get("k_iqr") if measured else None,
        e_mpa=_num(result.get("E_MPa")) if measured else None,
        e_iqr=result.get("E_iqr") if measured else None,
        shore_a=_num(result.get("shore_A")) if measured else None,
        uncertainty_pct=_num(result.get("uncertainty_pct")) if measured
        else None,
        n_presses=int(result["n"]) if measured and result.get("n") is not None
        else None,
        per_tip=result.get("by_tip") or {},
        captures=sorted(captures or []),
        skipped_captures=skipped_captures or [],
        capture_fingerprint=capture_fingerprint,
        job_timestamp=job_timestamp,
        method=method, method_commit=method_commit, source=source,
        measured_at=when,
    )
    db.add(row)

    unit = db.query(Devkit).filter(Devkit.device_axf_id == axf).first()
    if unit is None:
        unit = Devkit(device_axf_id=axf, device_type_id=DEVKIT_TYPE_ID,
                      flex_fp=fp, initialized_at=current_time(),
                      source="pad-stiffness")
        db.add(unit)
    unit.pad_stiffness_status = row.status
    unit.pad_stiffness_reason = row.reason
    unit.pad_shore_a = row.shore_a
    unit.pad_e_mpa = row.e_mpa
    unit.pad_k_n_per_mm = row.k_n_per_mm
    unit.pad_stiffness_tip_mm = row.tip_mm
    unit.pad_stiffness_uncertainty_pct = row.uncertainty_pct
    unit.pad_stiffness_n_presses = row.n_presses
    unit.pad_stiffness_measured_at = when
    return row


# --- response shapes ---------------------------------------------------------

class PadStiffnessSummary(BaseModel):
    """The latest measurement, as carried on the devkits row."""
    status: Optional[str] = None            # None = never measured
    reason: Optional[str] = None
    shore_a: Optional[float] = None
    e_mpa: Optional[float] = None
    k_n_per_mm: Optional[float] = None
    tip_mm: Optional[float] = None
    uncertainty_pct: Optional[float] = None
    n_presses: Optional[int] = None
    measured_at: Optional[datetime] = None

    @classmethod
    def from_devkit(cls, unit) -> Optional["PadStiffnessSummary"]:
        if unit is None or getattr(unit, "pad_stiffness_status", None) is None:
            return None
        return cls(status=unit.pad_stiffness_status,
                   reason=unit.pad_stiffness_reason,
                   shore_a=unit.pad_shore_a, e_mpa=unit.pad_e_mpa,
                   k_n_per_mm=unit.pad_k_n_per_mm,
                   tip_mm=unit.pad_stiffness_tip_mm,
                   uncertainty_pct=unit.pad_stiffness_uncertainty_pct,
                   n_presses=unit.pad_stiffness_n_presses,
                   measured_at=unit.pad_stiffness_measured_at)


class DevkitPadStiffnessResponse(BaseModel):
    """One history row."""
    id: int
    device_axf_id: str
    flex_fp: Optional[str] = None
    status: str
    reason: Optional[str] = None
    tip_mm: Optional[float] = None
    k_n_per_mm: Optional[float] = None
    k_iqr: Optional[List[float]] = None
    e_mpa: Optional[float] = None
    e_iqr: Optional[List[float]] = None
    shore_a: Optional[float] = None
    uncertainty_pct: Optional[float] = None
    n_presses: Optional[int] = None
    per_tip: Optional[Dict[str, dict]] = None
    captures: Optional[List[str]] = None
    skipped_captures: Optional[List[dict]] = None
    capture_fingerprint: Optional[str] = None
    job_timestamp: Optional[int] = None
    method: str
    method_commit: str
    source: str
    measured_at: datetime

    class Config:
        from_attributes = True
