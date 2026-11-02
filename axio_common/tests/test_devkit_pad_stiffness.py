"""Pad stiffness history + the devkits summary (Dyno #1301). SQLite in memory."""
import pytest

import sqlalchemy

_orig = sqlalchemy.create_engine


def _patched(url, *args, **kwargs):
    if str(url).startswith('sqlite'):
        for k in ('max_overflow', 'pool_timeout', 'pool_size', 'connect_args'):
            kwargs.pop(k, None)
    return _orig(url, *args, **kwargs)


sqlalchemy.create_engine = _patched

from sqlalchemy.orm import sessionmaker  # noqa: E402

from axio_common.database import Base  # noqa: E402
from axio_common.models import (  # noqa: E402
    Devkit, DevkitAssignmentHistory, DevkitPadStiffness, PadStiffnessSummary,
    record_pad_stiffness,
)

KIT = "18.6896a5c7"
MEASURED = {"tip": "d4", "tip_mm": 4.0, "k_N_per_mm": 28.64,
            "k_iqr": [27.9, 29.3], "n": 12, "E_MPa": 2.355,
            "E_iqr": [2.29, 2.41], "shore_A": 49.2, "uncertainty_pct": 5.0,
            "by_tip": {"d4": {"k_N_per_mm": 28.64, "n": 12},
                       "d8": {"k_N_per_mm": 95.7, "n": 17}}}
NOT = {"by_tip": {}, "reason": "no 2, 4 or 6 mm cylinder with >= 8 usable "
                                "presses (no cylinder presses)"}


@pytest.fixture
def db():
    engine = _orig("sqlite://")
    Base.metadata.create_all(engine, tables=[
        Devkit.__table__, DevkitAssignmentHistory.__table__,
        DevkitPadStiffness.__table__])
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _rec(db, axf, result, **kw):
    kw.setdefault("method", "stiffness_tr")
    kw.setdefault("method_commit", "46d2ca9")
    kw.setdefault("source", "auto")
    row = record_pad_stiffness(db, axf, result, **kw)
    db.commit()
    return row


def test_measured_row_creates_the_unit_and_its_summary(db):
    row = _rec(db, "18-6896A5C7", MEASURED, captures=["b", "a"],
               job_timestamp=20261101120000)
    assert row.device_axf_id == KIT and row.flex_fp == "6896a5c7"
    assert row.status == "measured" and row.reason is None
    assert row.captures == ["a", "b"]
    assert row.per_tip["d8"]["k_N_per_mm"] == 95.7
    unit = db.query(Devkit).one()
    assert unit.device_axf_id == KIT and unit.source == "pad-stiffness"
    s = PadStiffnessSummary.from_devkit(unit)
    assert (s.status, s.e_mpa, s.shore_a, s.tip_mm, s.n_presses) == \
        ("measured", 2.355, 49.2, 4.0, 12)


def test_not_measured_keeps_the_reason_and_no_numbers(db):
    _rec(db, KIT, MEASURED)
    _rec(db, KIT, NOT)
    rows = db.query(DevkitPadStiffness).order_by(DevkitPadStiffness.id).all()
    assert [r.status for r in rows] == ["measured", "not_measured"]
    assert rows[1].e_mpa is None and rows[1].shore_a is None
    unit = db.query(Devkit).one()
    assert unit.pad_stiffness_status == "not_measured"
    assert unit.pad_e_mpa is None
    assert "cylinder" in unit.pad_stiffness_reason


def test_refuses_the_sentinel_other_types_and_bad_sources(db):
    assert _rec(db, "18.00000000", MEASURED) is None
    assert _rec(db, "07.12345678", MEASURED) is None
    with pytest.raises(ValueError):
        record_pad_stiffness(db, KIT, MEASURED, method="m", method_commit="c",
                             source="guess")
    assert db.query(DevkitPadStiffness).count() == 0
    assert db.query(Devkit).count() == 0


def test_never_measured_has_no_summary(db):
    db.add(Devkit(device_axf_id=KIT, device_type_id="18"))
    db.commit()
    assert PadStiffnessSummary.from_devkit(db.query(Devkit).one()) is None
