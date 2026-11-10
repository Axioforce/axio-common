"""The load cell's end-of-line record, shippability and certificate data (#1423).

SQLite in memory, only the load cell tables. The rules these pin, each with a
test that fails when the rule is broken:

  - shippable needs all three: calibrated (a pack recorded), an on-board EOL
    pass with THAT pack's CRC, and no calibration recorded after the pass;
  - a later calibration revokes it, even one re-installing the same pack;
  - a simulated pass, the server's automatic row and a pass for another pack
    never ship a unit;
  - the certificate's data comes from those same rows, and says provisional.
"""
import zlib
from datetime import datetime, timedelta, timezone

import pytest

original_create_engine = None


def patched_create_engine(url, *args, **kwargs):
    if str(url).startswith('sqlite'):
        for k in ('max_overflow', 'pool_timeout', 'pool_size', 'connect_args'):
            kwargs.pop(k, None)
    return original_create_engine(url, *args, **kwargs)


import sqlalchemy

if getattr(sqlalchemy.create_engine, "__name__", "") == "patched_create_engine":
    original_create_engine = sqlalchemy.create_engine.__globals__[
        "original_create_engine"]
else:
    original_create_engine = sqlalchemy.create_engine
    sqlalchemy.create_engine = patched_create_engine

from sqlalchemy.orm import sessionmaker

from axio_common.database import Base
from axio_common.models import (
    LOADCELL_AUTO_EOL_TOOL, LOADCELL_EOL_CHECKS, LOADCELL_EOL_TOOL,
    LoadCellEolRequest, LoadCellEolResponse, LoadCellEolResult,
    job_outputs, job_recipe, loadcell_certificate, loadcell_shippability,
    required_outputs,
)
from axio_common.models.loadcell import (
    LOADCELL_TYPE_IDS, LoadCellCalibration, LoadCellUnit,
    LoadCellUnitTypeHistory,
)

UID = "1a001b001750314751333420"
SERIAL = f"{zlib.crc32(bytes.fromhex(UID)):08x}"
assert SERIAL == "e732d808"
T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
CRC_A, CRC_B = "dcf30ce3", "0cdb1dce"


@pytest.fixture
def db():
    engine = original_create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        LoadCellUnit.__table__, LoadCellUnitTypeHistory.__table__,
        LoadCellCalibration.__table__, LoadCellEolResult.__table__])
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _unit(db):
    unit, _ = LoadCellUnit.register(db, SERIAL, UID)
    db.commit()
    return unit


def _calibrate(db, unit, crc=CRC_A, at=T0, job="job-a"):
    row = unit.record_calibration(
        db, "1a", model_version="20261110", model_crc32=crc,
        calibration_date=at, operator="Stephen", recorded_by="operator",
        job_id=job, job_timestamp=20261110120000)
    row.recorded_at = at
    db.commit()
    return row


FIGURES = {"outputs": ["Fx", "Fy", "Fz", "Tx", "Ty", "Tz"],
           "rated": [0.0] * 6,
           "all": {"n": 100, "mae": [0.05, 0.05, 0.1, 0.0013, 0.0023, 0.0006],
                   "max_abs": [0.3, 0.3, 0.5, 0.01, 0.01, 0.004],
                   "rmse": [0.06, 0.06, 0.12, 0.0016, 0.003, 0.0008],
                   "mae_pct_fs": [None] * 6, "max_pct_fs": [None] * 6,
                   "rmse_pct_fs": [None] * 6},
           "by_activity": {"GRID": {"n": 60}},
           "repeatability": {"measured": False, "sd": None},
           "rows": {"scored": 100}}


def _eol(db, *, crc=CRC_A, at=T0 + timedelta(hours=1), passed=True,
         simulated=False, tool=LOADCELL_EOL_TOOL, job="job-a", failing=()):
    checks = {c: {"passed": c not in failing, "detail": ""}
              for c in LOADCELL_EOL_CHECKS}
    row = LoadCellEolResult(
        serial=SERIAL, mcu_uid=UID, device_type_id="1a",
        device_axf_id=f"1a.{SERIAL}", job_id=job, model_crc32=crc,
        model_version="20261110", passed=passed, simulated=simulated,
        checks=checks, figures=FIGURES, thresholds={"provisional": True},
        provisional=True, blockers=["PROVISIONAL_THRESHOLDS (owner: Stephen) -- x"],
        operator="sam", station="eol-1", tool=tool, firmware_version="0.4.0",
        completed_at=at, recorded_at=at)
    db.add(row)
    db.commit()
    return row


# --- the three conditions ----------------------------------------------------

def test_calibrated_plus_a_pass_with_its_pack_ships(db):
    unit = _unit(db)
    cal = _calibrate(db, unit)
    row = _eol(db)
    s = loadcell_shippability(db, SERIAL)
    assert s["shippable"], s["reason"]
    assert (s["eol_result_id"], s["calibration_id"], s["model_crc32"]) == \
        (row.id, cal.id, CRC_A)
    assert "provisional" in s["reason"]


def test_not_calibrated_never_ships(db):
    _unit(db)
    _eol(db)                                    # a pass, but no pack recorded
    s = loadcell_shippability(db, SERIAL)
    assert not s["shippable"] and "not calibrated" in s["reason"]


def test_a_pass_with_another_pack_does_not_ship(db):
    unit = _unit(db)
    _calibrate(db, unit, crc=CRC_B)
    _eol(db, crc=CRC_A)
    s = loadcell_shippability(db, SERIAL)
    assert not s["shippable"] and f"now carries pack {CRC_B}" in s["reason"]


def test_a_later_calibration_revokes_shippable(db):
    unit = _unit(db)
    _calibrate(db, unit)
    _eol(db, at=T0 + timedelta(hours=1))
    assert loadcell_shippability(db, SERIAL)["shippable"]
    # the same pack installed again, after the pass: needs a new EOL
    _calibrate(db, unit, at=T0 + timedelta(hours=2))
    s = loadcell_shippability(db, SERIAL)
    assert not s["shippable"] and "calibrated again" in s["reason"]
    # ...and a new pass restores it
    _eol(db, at=T0 + timedelta(hours=3))
    assert loadcell_shippability(db, SERIAL)["shippable"]


def test_a_new_pack_after_the_pass_revokes_shippable(db):
    unit = _unit(db)
    _calibrate(db, unit)
    _eol(db)
    _calibrate(db, unit, crc=CRC_B, at=T0 + timedelta(hours=2), job="job-b")
    s = loadcell_shippability(db, SERIAL)
    assert not s["shippable"] and CRC_B in s["reason"]


@pytest.mark.parametrize("kw,words", [
    (dict(simulated=True), "simulator"),
    (dict(tool=LOADCELL_AUTO_EOL_TOOL, passed=False), "automatic"),
    (dict(passed=False, failing=("tare",)), "Failing checks: tare"),
])
def test_what_never_ships(db, kw, words):
    unit = _unit(db)
    _calibrate(db, unit)
    _eol(db, **kw)
    s = loadcell_shippability(db, SERIAL)
    assert not s["shippable"] and words in s["reason"], s["reason"]


def test_an_automatic_row_stored_as_passed_still_never_ships(db):
    unit = _unit(db)
    _calibrate(db, unit)
    _eol(db, tool=LOADCELL_AUTO_EOL_TOOL, passed=True)
    assert not loadcell_shippability(db, SERIAL)["shippable"]


def test_a_failure_after_the_pass_does_not_unship(db):
    """The pass is evidence about that pack; a later bench failure (a cable)
    is on file beside it. Shippability names the pass."""
    unit = _unit(db)
    _calibrate(db, unit)
    good = _eol(db, at=T0 + timedelta(hours=1))
    _eol(db, at=T0 + timedelta(hours=2), passed=False, failing=("stream",))
    s = loadcell_shippability(db, SERIAL)
    assert s["shippable"] and s["eol_result_id"] == good.id


def test_unregistered_and_malformed(db):
    assert "not registered" in loadcell_shippability(db, "00000001")["reason"]
    assert not loadcell_shippability(db, "nope")["shippable"]


# --- the wire shape ----------------------------------------------------------

def test_the_request_takes_the_sdk_document_as_is():
    doc = {"schema": 1, "serial": SERIAL, "mcu_uid": UID, "passed": True,
           "checks": {}, "model_crc32": CRC_A, "provisional": True,
           "host": {"platform": "x"}, "job_timestamp": 20261110120000}
    req = LoadCellEolRequest.model_validate(doc)
    assert req.result_schema == 1 and req.job_timestamp == 20261110120000


def test_the_response_round_trips_a_row(db):
    unit = _unit(db)
    _calibrate(db, unit)
    row = _eol(db)
    got = LoadCellEolResponse.model_validate(row)
    assert got.model_crc32 == CRC_A and got.figures["all"]["n"] == 100
    assert got.model_dump(by_alias=True)["schema"] == 1


# --- the certificate's data --------------------------------------------------

def test_certificate_data(db):
    unit = _unit(db)
    cal = _calibrate(db, unit)
    row = _eol(db)
    c = loadcell_certificate(db, SERIAL)
    assert c.shippable and c.eol_result_id == row.id
    assert (c.serial, c.device_type_id, c.device_axf_id) == \
        (SERIAL, "1a", f"1a.{SERIAL}")
    assert c.type_name.startswith("Load Cell")
    assert (c.model_version, c.model_crc32, c.calibration_id) == \
        ("20261110", CRC_A, cal.id)
    assert c.provisional and c.eol_operator == "sam"
    assert [a.output for a in c.axes] == ["Fx", "Fy", "Fz", "Tx", "Ty", "Tz"]
    assert c.axes[2].mae == 0.1 and c.axes[2].rated is None
    assert c.axes[3].unit == "N m" and c.axes[0].repeatability is None
    assert c.eol_checks == {k: True for k in LOADCELL_EOL_CHECKS}
    assert loadcell_certificate(db, "00000001") is None


def test_certificate_of_an_unshippable_unit_says_why(db):
    unit = _unit(db)
    _calibrate(db, unit)
    _eol(db, passed=False, failing=("accuracy",))
    c = loadcell_certificate(db, SERIAL)
    assert not c.shippable and "did not pass" in c.shippable_reason
    assert c.eol_passed is False and c.eol_checks["accuracy"] is False


# --- what a load cell job produces (#1423) -----------------------------------

def test_load_cell_types_need_the_six_outputs():
    for t in LOADCELL_TYPE_IDS:
        assert required_outputs(t) == ("force", "moment")


def test_load_cell_recipes_produce_force_and_moment():
    for name in ("joint_v1", "split_v1", "joint_t_v1", "split_t_v1",
                 "joint_k1_v1", "split_k1_v1"):
        cfg = {"LOADCELL_RECIPE": name, "LOADCELL_SERIAL": SERIAL}
        assert job_outputs("force", cfg) == {"force", "moment"}
        assert job_recipe(cfg) == name
    assert job_outputs("force", {"LOADCELL_RECIPE": "joint_v9"}) is None
    # a dev-kit recipe still wins its own table
    assert job_outputs("force", {"DEVKIT_RECIPE": "set_xf_v1"}) == \
        {"force", "cop", "field"}
