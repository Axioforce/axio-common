"""Load cell captures in the bucket, their index, the training job, and the
calibration record (#1408).

Pure functions and SQLite in memory; the one network test (the real
tests.txt of the first board's test upload) is skipped without Tigris
credentials and compares against the committed copy of that file.

The traps these pin:

  - a `test-<iso-date>` upload is listed and flagged, and is **never**
    calibration data: the job builder refuses it;
  - the cache layout round-trips through `bucket_key_for_cache_path`, test
    segments included, so nothing ever uploads a cache path as a key;
  - the job carries exactly LOADCELL_RECIPE / LOADCELL_SERIAL / LOADCELL_TYPE
    on top of the ordinary job shape, refuses type 0 and refuses 19 unless
    the cell is custom;
  - a calibration types the unit once; a different type later is refused and
    writes nothing; the job UID stays an integer.
"""
import json
import os
import zlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

original_create_engine = None


def patched_create_engine(url, *args, **kwargs):
    if str(url).startswith('sqlite'):
        for k in ('max_overflow', 'pool_timeout', 'pool_size', 'connect_args'):
            kwargs.pop(k, None)
    return original_create_engine(url, *args, **kwargs)


import sqlalchemy

original_create_engine = sqlalchemy.create_engine
sqlalchemy.create_engine = patched_create_engine

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from axio_common.database import Base
from axio_common.models.loadcell import (
    LoadCellCalibration, LoadCellCalibrationResponse, LoadCellTypeAlreadySet,
    LoadCellUnit, LoadCellUnitTypeHistory,
)
from axio_common.models.loadcell_capture import (
    LOADCELL_JOB_KEYS, LoadCellCaptureFile, LoadCellCaptureSession,
    loadcell_job_config, loadcell_job_fields, loadcell_job_type,
)
from axio_common.storage import loadcell as L
from axio_common.storage import storage_core as sc

FIXTURE = Path(__file__).parent / "data" / \
    "loadcell_fe059e4b_test-2026-11-09_tests.txt"
TEST_SESSION = "loadcell/fe059e4b/test-2026-11-09/"

S = "15322548"
TR = f"loadcell/{S}/2026-11-10/train/{S}-TR-GRID_11.10.2026.csv.gz"
TR2 = f"loadcell/{S}/2026-11-12/train/{S}-TR-ARMX_11.12.2026.csv.gz"
TE = f"loadcell/{S}/2026-11-10/test/{S}-TE-GRID_11.10.2026.csv.gz"
SIDECAR = f"loadcell/{S}/2026-11-10/axiosaurus/{S}-TR-GRID_11.10.2026_loadcell.json"
TESTS_TXT = f"loadcell/{S}/2026-11-10/tests.txt"
TEST_UPLOAD = f"loadcell/{S}/test-2026-11-10/train/{S}-TR-GRID_11.10.2026.csv.gz"


# --- keys --------------------------------------------------------------------

def test_parse_capture_keys():
    p = L.parse_loadcell_key(TR)
    assert (p.serial, p.part, p.segment, p.iso_date, p.is_test,
            p.activity, p.is_capture) == \
        (S, "train", "2026-11-10", "2026-11-10", False, "TR-GRID", True)
    q = L.parse_loadcell_key(TE)
    assert (q.part, q.activity, q.is_capture) == ("test", "TE-GRID", True)
    assert q.session_prefix == f"loadcell/{S}/2026-11-10/"


def test_parse_the_other_parts():
    assert L.parse_loadcell_key(TESTS_TXT).part == "tests_txt"
    assert L.parse_loadcell_key(SIDECAR).part == "sidecar"
    assert L.parse_loadcell_key(SIDECAR).activity is None
    m = L.parse_loadcell_key(f"loadcell/{S}/models/x/{S}-20261110120000-1-f.h5")
    assert (m.part, m.segment, m.is_test) == ("model", None, False)
    assert L.parse_loadcell_key(f"loadcell/{S}/2026-11-10/notes.md").part == \
        "other"


def test_a_test_upload_is_flagged():
    p = L.parse_loadcell_key(TEST_UPLOAD)
    assert (p.is_test, p.segment, p.iso_date, p.part) == \
        (True, "test-2026-11-10", "2026-11-10", "train")


@pytest.mark.parametrize("key", [
    f"loadcell/{S}/2026-11-10/train/",                 # folder marker
    f"loadcell/{S.upper()}X/2026-11-10/tests.txt",    # not a serial
    "loadcell/FE059E4B/2026-11-10/tests.txt",          # uppercase: not the spelling
    f"loadcell/{S}/11.10.2026/train/a.csv.gz",         # a cache date
    f"loadcell/{S}/testing/train/a.csv.gz",
    f"loadcell/{S}/test-11.10.2026/train/a.csv.gz",
    "10/10-00000002/2026-05-13/train/10-00000002-TR-BER_05.13.2026.csv.gz",
    f"loadcell/{S}",
])
def test_not_a_load_cell_session_key(key):
    p = L.parse_loadcell_key(key)
    assert p is None or p.part == "model"


def test_date_segments():
    assert L.parse_date_segment("2026-11-10") == ("2026-11-10", False)
    assert L.parse_date_segment("test-2026-11-09") == ("2026-11-09", True)
    assert L.parse_date_segment("models") is None
    assert L.date_segment("2026-11-09", test=True) == "test-2026-11-09"
    with pytest.raises(ValueError):
        L.loadcell_session_prefix(S, "11.10.2026")
    with pytest.raises(ValueError):
        L.loadcell_serial_prefix("1532254")


@pytest.mark.parametrize("key", [TR, TE, TESTS_TXT, SIDECAR, TEST_UPLOAD,
                                 TEST_SESSION + "tests.txt"])
def test_cache_layout_round_trips(key, tmp_path):
    rel = L.loadcell_cache_relative(key)
    assert "calibration_data" in rel or not L.parse_loadcell_key(key).is_capture
    assert sc.bucket_key_for_cache_path(tmp_path / rel, tmp_path) == key
    assert sc.bucket_key_for_cache_path(rel) == key


def test_cache_relative_spelling():
    assert L.loadcell_cache_relative(TR) == \
        f"loadcell/{S}/11.10.2026/calibration_data/train/{S}-TR-GRID_11.10.2026.csv"
    assert L.loadcell_cache_relative(TEST_UPLOAD).startswith(
        f"loadcell/{S}/test-2026-11-10/calibration_data/train/")


def test_session_for_cache_path():
    for path in (f"C:/u/.axio-cache/loadcell/{S}/11.10.2026/calibration_data/train/a.csv",
                 f"loadcell/{S}/11.10.2026",
                 f"/home/x/cache/loadcell/{S}/test-2026-11-09/tests.txt"):
        got = L.loadcell_session_for_cache_path(path)
        assert got is not None and got[0] == S
    assert L.loadcell_session_for_cache_path(
        f"C:/c/loadcell/{S}/11.10.2026/x") == (S, "2026-11-10")
    assert L.loadcell_session_for_cache_path(
        f"/c/loadcell/{S}/test-2026-11-09/x") == (S, "test-2026-11-09")
    assert L.loadcell_session_for_cache_path("10/10-00000002/05.13.2026") is None
    assert L.loadcell_session_for_cache_path(f"loadcell/{S}/models/x") is None


# --- tests.txt ---------------------------------------------------------------

def _check_first_board_tests_txt(parsed):
    h = parsed["header"]
    assert h["serial"] == "fe059e4b"
    # The UID the board reported, and the serial it must have: computed here.
    assert f"{zlib.crc32(bytes.fromhex(h['mcu_uid'])):08x}" == h["serial"]
    assert h["device_type"].startswith("00")
    assert h["calibrated_by"]
    assert h["reference"].startswith("SIMULATED")
    assert parsed["activities"] == ["TR-GRID", "TR-ARMX", "TR-EDGETZ"]
    assert set(parsed["descriptions"]) == {"TR-GRID", "TR-ARMX", "TR-EDGETZ"}
    # an activity line is not a header field
    assert not any(k.startswith("tr_") for k in h)


def test_tests_txt_of_the_first_board():
    _check_first_board_tests_txt(
        L.parse_loadcell_tests_txt(FIXTURE.read_text(encoding="utf-8")))


@pytest.mark.skipif(not (os.environ.get("AWS_ACCESS_KEY_ID")
                         and os.environ.get("AWS_SECRET_ACCESS_KEY")),
                    reason="no Tigris credentials (read-only check of the "
                           "real object)")
def test_tests_txt_from_the_bucket(tmp_path, monkeypatch):
    """The real object, downloaded read-only with the storage helpers."""
    monkeypatch.setenv("AXIO_STORAGE_BACKEND", "s3")
    listing = L.list_loadcell_session("fe059e4b", "test-2026-11-09")
    assert listing.is_test and listing.tests_txt == TEST_SESSION + "tests.txt"
    assert listing.train and not listing.test and listing.sidecars
    local = sc.ensure_local(listing.tests_txt, tmp_path)
    live = L.parse_loadcell_tests_txt(local.read_text(encoding="utf-8"))
    _check_first_board_tests_txt(live)
    assert live == L.parse_loadcell_tests_txt(
        FIXTURE.read_text(encoding="utf-8"))


def test_tests_txt_odd_input():
    assert L.parse_loadcell_tests_txt("") == \
        {"header": {}, "activities": [], "descriptions": {}}
    got = L.parse_loadcell_tests_txt("Serial: FE059E4B\nnonsense\nTE-X: y\n")
    assert got["header"] == {"serial": "fe059e4b"}
    assert got["activities"] == ["TE-X"]


# --- listing (backend faked) -------------------------------------------------

def test_listing_keeps_test_sessions_apart(monkeypatch):
    keys = [TR, TE, TESTS_TXT, SIDECAR, TR2, TEST_UPLOAD,
            f"loadcell/{S}/2026-11-10/train/",
            f"loadcell/{S}/models/11.10.2026/best_model/x.h5"]
    monkeypatch.setattr(sc, "_list_keys_recursive",
                        lambda prefix: sorted(k for k in keys
                                              if k.startswith(prefix)))
    assert L.list_loadcell_serials() == [S]
    assert L.list_loadcell_sessions(S) == ["2026-11-10", "2026-11-12"]
    assert L.list_loadcell_sessions(S, include_test=True) == \
        ["2026-11-10", "2026-11-12", "test-2026-11-10"]
    one = L.list_loadcell_session(S, "2026-11-10")
    assert (one.train, one.test, one.tests_txt, one.sidecars) == \
        ([TR], [TE], TESTS_TXT, [SIDECAR])
    assert not one.is_test
    assert L.list_loadcell_session(S, "test-2026-11-10").is_test


# --- the job -----------------------------------------------------------------

def test_job_config_contract():
    c = loadcell_job_config(S, "1A", "joint_v1", train_keys=[TR, TR2, TR],
                            test_keys=[TE], notes="first")
    assert {k: c[k] for k in LOADCELL_JOB_KEYS} == {
        "LOADCELL_RECIPE": "joint_v1", "LOADCELL_SERIAL": S,
        "LOADCELL_TYPE": "1a"}
    assert c["TRAIN_INPUT_DIR"] == [L.loadcell_cache_relative(TR),
                                    L.loadcell_cache_relative(TR2)]
    assert c["TEST_INPUT_DIR"] == [L.loadcell_cache_relative(TE)]
    assert c["SELECTED_DIRS"] == [f"loadcell/{S}/11.10.2026",
                                  f"loadcell/{S}/11.12.2026"]
    assert c["RESULTS_DIR"] == f"loadcell/{S}/models/11.10.12.2026"
    assert c["NN_RESULTS_DIR"] == c["RESULTS_DIR"] + "/nn_results/force_vector"
    assert c["DEVICE_ID"] == f"1a-{S}" and c["NOTES"] == "first"
    # every key the Neuralizer refuses a job without
    for k in ("RESULTS_DIR", "OUTPUT_TYPE", "TRAIN_INPUT_DIR",
              "TEST_INPUT_DIR", "TRAIN_SAMPLING_RATIO", "TEST_SAMPLING_RATIO",
              "TEST_SIZE", "RANDOM_STATE", "LEARNING_RATES", "HIDDEN_LAYERS",
              "ACTIVATIONS", "OPTIMIZERS", "EPOCHS", "BATCH_SIZES"):
        assert k in c, k
    assert json.loads(json.dumps(c)) == c
    assert loadcell_job_fields(json.dumps(c)) == \
        {"recipe": "joint_v1", "serial": S, "type": "1a"}
    assert loadcell_job_fields({"DEVICE_ID": "10-00000002"}) is None


@pytest.mark.parametrize("train,test,why", [
    ([TEST_UPLOAD], [TE], "test data"),
    ([TR], [TE.replace(S, "fe059e4b")], "not a capture of"),
    ([TR, SIDECAR], [TE], "not a train/test capture"),
    ([TR, TESTS_TXT], [TE], "not a train/test capture"),
    ([TE], [TE], "listed as test"),
    ([TR], [TR], "listed as test"),
    ([], [TE], "no train capture"),
    ([TR], [], "no test capture"),
])
def test_job_refuses_inputs(train, test, why):
    with pytest.raises(ValueError, match=why):
        loadcell_job_config(S, "1a", "joint_v1", train_keys=train,
                            test_keys=test)


def test_job_types():
    for t in ("1a", "1b", "1c", "1d", "1e", "1f", 0x1A):
        assert loadcell_job_type(t) in ("1a", "1b", "1c", "1d", "1e", "1f")
    for t in (0, "0", "00", "18", "20", None):
        with pytest.raises(ValueError):
            loadcell_job_type(t)
    with pytest.raises(ValueError, match="custom"):
        loadcell_job_type("19")
    assert loadcell_job_type("19", custom=True) == "19"
    with pytest.raises(ValueError, match="recipe"):
        loadcell_job_config(S, "1a", "Joint V1", train_keys=[TR],
                            test_keys=[TE])


# --- tables ------------------------------------------------------------------

@pytest.fixture
def db():
    engine = original_create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        LoadCellUnit.__table__, LoadCellUnitTypeHistory.__table__,
        LoadCellCalibration.__table__, LoadCellCaptureSession.__table__,
        LoadCellCaptureFile.__table__])
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


UID = "2a001d001750314751333420"          # the first board, from its tests.txt
SERIAL = f"{zlib.crc32(bytes.fromhex(UID)):08x}"
WHEN = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def _calibrate(db, unit, type_id="1a", **kw):
    args = dict(model_version="20261110", model_crc32="0badc0de",
                calibration_date=WHEN, operator="Stephen",
                recorded_by="operator (Stephen)", job_id="job-1",
                job_timestamp=20261110120000, run_number=1,
                model_key=f"loadcell/{unit.serial}/models/11.10.2026/m-f.h5",
                pack_key=f"loadcell/{unit.serial}/models/11.10.2026/m-f.pack",
                heldout={"TE-all": {"mae": [0.1, 0.2, 0.3, 1.0, 2.0, 3.0]}})
    args.update(kw)
    return unit.record_calibration(db, type_id, **args)


def test_calibration_types_the_unit_once(db):
    unit, _ = LoadCellUnit.register(db, SERIAL, UID)
    db.commit()
    row = _calibrate(db, unit)
    db.commit()
    assert (unit.device_type_id, unit.device_axf_id, unit.model_version,
            unit.model_crc32, unit.operator) == \
        ("1a", f"1a.{SERIAL}", "20261110", "0badc0de", "Stephen")
    assert unit.calibration_date is not None
    got = LoadCellCalibrationResponse.model_validate(row)
    assert got.job_timestamp == 20261110120000
    assert isinstance(got.job_timestamp, int)
    assert got.heldout["TE-all"]["mae"][5] == 3.0
    hist = db.query(LoadCellUnitTypeHistory).all()
    assert [(h.previous_type, h.new_type) for h in hist] == [(None, "1a")]

    # a re-install of the same type: a second record, no new history
    _calibrate(db, unit, model_version="20261111", model_crc32="00000001")
    db.commit()
    assert unit.model_version == "20261111"
    assert db.query(LoadCellCalibration).count() == 2
    assert db.query(LoadCellUnitTypeHistory).count() == 1


def test_a_different_type_is_refused_and_writes_nothing(db):
    unit, _ = LoadCellUnit.register(db, SERIAL, UID)
    _calibrate(db, unit)
    db.commit()
    with pytest.raises(LoadCellTypeAlreadySet):
        _calibrate(db, unit, type_id="1b", model_version="20261112")
    db.rollback()
    assert (unit.device_type_id, unit.model_version) == ("1a", "20261110")
    assert db.query(LoadCellCalibration).count() == 1


@pytest.mark.parametrize("kw,why", [
    (dict(model_crc32=None), "model_crc32"),
    (dict(model_version=None), "model_version"),
    (dict(model_version="2026-11-10"), "YYYYMMDD"),
    (dict(calibration_date=None), "calibration_date"),
    (dict(operator=" "), "operator"),
    (dict(recorded_by=""), "recorded_by"),
    (dict(job_timestamp=1731240000), "job UID"),
    (dict(job_timestamp="20261110120000"), "job UID"),
    (dict(run_number=0), "run"),
    (dict(model_key="18/18-542d8caf/models/x.h5"), "model_key"),
    (dict(pack_key="loadcell/00000000/models/x.pack"), "pack_key"),
    (dict(heldout=[1, 2]), "heldout"),
])
def test_calibration_validation_writes_nothing(db, kw, why):
    unit, _ = LoadCellUnit.register(db, SERIAL, UID)
    db.commit()
    with pytest.raises(ValueError, match=why):
        _calibrate(db, unit, **kw)
    db.rollback()
    assert unit.device_type_id is None and unit.model_version is None
    assert db.query(LoadCellCalibration).count() == 0
    assert db.query(LoadCellUnitTypeHistory).count() == 0


def test_calibration_refuses_type_zero(db):
    unit, _ = LoadCellUnit.register(db, SERIAL, UID)
    with pytest.raises(ValueError):
        _calibrate(db, unit, type_id=0)
    assert unit.device_type_id is None


def test_capture_index_is_unique_per_serial_and_segment(db):
    def sess(seg, test):
        return LoadCellCaptureSession(
            serial=S, date_segment=seg, date_iso=seg[-10:], is_test=test,
            bucket_prefix=f"loadcell/{S}/{seg}/")
    db.add(sess("2026-11-10", False))
    db.add(sess("test-2026-11-10", True))
    db.commit()
    s = db.query(LoadCellCaptureSession).filter_by(is_test=False).one()
    s.files.append(LoadCellCaptureFile(bucket_key=TR, filename="a",
                                       part="train", activity_id="TR-GRID"))
    db.commit()
    db.add(sess("2026-11-10", False))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.delete(s)
    db.commit()
    assert db.query(LoadCellCaptureFile).count() == 0
