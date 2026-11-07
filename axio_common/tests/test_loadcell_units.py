"""Load cell unit records (contract 6, firmware `docs/SERVER_RECORDS.md`, #1380).

SQLite in memory, only the two load cell tables -- no Postgres, no network.

The traps these pin:

  - **the serial is a function of the UID**: CRC-32 (IEEE, zlib's) over the
    12 UID bytes in IDENTIFY order. Checked against zlib AND against a
    bitwise CRC-32 written here, so a wrong byte order or a wrong polynomial
    cannot pass by agreeing with itself.
  - **a CRC collision is refused**, and so is a UID under another serial;
    nothing is written either way.
  - **the type is set once**, through `set_type` only, and a forced re-type
    keeps the old type in the history.

The first board's serial is `fe059e4b`, but its UID is not written down
anywhere, so these tests use UIDs of their own rather than invent it.
"""
import zlib

import pytest

# SQLite rejects the pool kwargs axio_common.database passes at import time.
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
    LOADCELL_TYPE_IDS, LoadCellIdentityConflict, LoadCellSerialMismatch,
    LoadCellTypeAlreadySet, LoadCellUnit, LoadCellUnitResponse,
    LoadCellUnitTypeHistory, LoadCellUnitTypeHistoryResponse, device_id_for,
    format_mcu_uid, format_serial, format_type_id, is_loadcell_type,
    serial_from_uid,
)
from axio_common.storage.activities import (
    TYPE_ID_TO_FAMILY, default_expected_for, family_for_device_id,
    family_for_type_id, parse_activity_from_key,
)

# A UID of our own, built the way IDENTIFY carries it: three 32-bit words,
# word 0 first, each little-endian.
W0, W1, W2 = 0x00450031, 0x3233510A, 0x31383836
RAW = W0.to_bytes(4, "little") + W1.to_bytes(4, "little") + W2.to_bytes(4, "little")
UID = RAW.hex()                                  # 24 lowercase hex
OTHER_RAW = bytes(range(1, 13))
OTHER_UID = OTHER_RAW.hex()


def crc32_bitwise(data: bytes) -> int:
    """CRC-32/IEEE (reflected, poly 0xEDB88320), written out bit by bit."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


SERIAL = f"{zlib.crc32(RAW):08x}"
OTHER_SERIAL = f"{zlib.crc32(OTHER_RAW):08x}"


@pytest.fixture
def db():
    engine = original_create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        LoadCellUnit.__table__, LoadCellUnitTypeHistory.__table__])
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


# --- spellings ---------------------------------------------------------------

def test_the_bitwise_reference_is_crc32_ieee():
    # The standard CRC-32/IEEE check value, so the reference itself is
    # pinned to the algorithm and not just to zlib.
    assert crc32_bitwise(b"123456789") == 0xCBF43926


def test_serial_from_uid_is_crc32_over_the_identify_bytes():
    assert serial_from_uid(UID) == SERIAL
    assert serial_from_uid(UID) == f"{crc32_bitwise(RAW):08x}"
    assert serial_from_uid(RAW) == SERIAL            # raw IDENTIFY bytes
    assert serial_from_uid(UID.upper()) == SERIAL
    # Word order matters: the same words in w2 w1 w0 order are another UID.
    reversed_words = (W2.to_bytes(4, "little") + W1.to_bytes(4, "little")
                      + W0.to_bytes(4, "little"))
    assert serial_from_uid(reversed_words) != SERIAL


def test_round_trip_bytes_to_uid_to_serial_to_device_id():
    uid = format_mcu_uid(RAW)
    assert uid == UID and len(uid) == 24 and uid == uid.lower()
    assert bytes.fromhex(uid) == RAW
    assert format_mcu_uid(int(uid, 16)) == uid
    serial = serial_from_uid(uid)
    assert format_serial(int(serial, 16)) == serial
    assert device_id_for(serial, "1A") == f"1a.{serial}"
    assert device_id_for(serial, 0x1F) == f"1f.{serial}"


def test_format_serial_is_eight_lowercase_hex():
    assert format_serial("FE059E4B") == "fe059e4b"
    assert format_serial(0xFE059E4B) == "fe059e4b"
    assert format_serial(0x1234) == "00001234"       # never "1234"
    assert format_serial(" 00001234 ") == "00001234"
    for bad in (None, True, -1, 1 << 32, "1234", "fe059e4bz", "fe059e4b0",
                "", 1.5, b"\x00" * 4):
        with pytest.raises(ValueError):
            format_serial(bad)


def test_format_mcu_uid_is_twenty_four_lowercase_hex():
    assert format_mcu_uid(UID.upper()) == UID
    for bad in (None, False, "", UID[:-1], UID + "0", "g" * 24, b"\x00" * 11,
                -1, 1 << 96):
        with pytest.raises(ValueError):
            format_mcu_uid(bad)


def test_types_and_the_uncalibrated_sentinel():
    assert LOADCELL_TYPE_IDS == ("19", "1a", "1b", "1c", "1d", "1e", "1f")
    for t in ("19", "1a", "1F", 0x1C, "0x1d"):
        assert is_loadcell_type(t)
    for t in (None, 0, "00", "18", "20", "17", "1g", True, ""):
        assert not is_loadcell_type(t)
    # The uncalibrated board (IDENTIFY type 0) has no device id at all.
    assert device_id_for(SERIAL, None) is None
    assert device_id_for(SERIAL, 0) is None
    assert device_id_for(SERIAL, "00") is None
    for t in ("18", "07", 0x20):
        with pytest.raises(ValueError):
            device_id_for(SERIAL, t)
    with pytest.raises(ValueError):
        format_type_id(0)
    with pytest.raises(ValueError):
        device_id_for("123", "1a")


# --- registration ------------------------------------------------------------

def test_register_creates_once_and_is_idempotent(db):
    row, created = LoadCellUnit.register(
        db, SERIAL.upper(), UID.upper(), board_rev="A",
        firmware_version="0.4.0", source="lc.py register")
    db.commit()
    assert created
    assert (row.serial, row.mcu_uid) == (SERIAL, UID)
    assert row.device_type_id is None and row.device_axf_id is None
    first = row.initialized_at

    row, created = LoadCellUnit.register(db, SERIAL, UID,
                                         firmware_version="0.5.0")
    db.commit()
    assert not created
    assert row.firmware_version == "0.5.0"
    assert row.board_rev == "A"                      # None erased nothing
    assert row.source == "lc.py register"
    assert row.initialized_at == first
    assert db.query(LoadCellUnit).count() == 1


def test_register_refuses_a_serial_that_is_not_the_uids_crc(db):
    with pytest.raises(LoadCellSerialMismatch, match="does not match"):
        LoadCellUnit.register(db, OTHER_SERIAL, UID)
    assert db.query(LoadCellUnit).count() == 0


def test_register_refuses_an_all_zero_uid(db):
    zero = "0" * 24
    with pytest.raises(ValueError, match="all zeros"):
        LoadCellUnit.register(db, serial_from_uid(zero), zero)
    assert db.query(LoadCellUnit).count() == 0


def test_register_refuses_the_crc_collision_and_writes_nothing(db):
    # Seeded directly: a row holding SERIAL under a different UID is what a
    # real CRC-32 collision looks like.
    db.add(LoadCellUnit(serial=SERIAL, mcu_uid=OTHER_UID))
    db.commit()
    with pytest.raises(LoadCellIdentityConflict) as e:
        LoadCellUnit.register(db, SERIAL, UID, notes="should not land")
    assert "another board" in str(e.value)
    assert UID in str(e.value) and OTHER_UID in str(e.value)
    assert e.value.existing_mcu_uid == OTHER_UID
    db.rollback()
    row = db.query(LoadCellUnit).one()
    assert (row.serial, row.mcu_uid, row.notes) == (SERIAL, OTHER_UID, None)


def test_register_refuses_a_uid_held_under_another_serial(db):
    db.add(LoadCellUnit(serial="0badc0de", mcu_uid=UID))
    db.commit()
    with pytest.raises(LoadCellIdentityConflict, match="another serial"):
        LoadCellUnit.register(db, SERIAL, UID)
    db.rollback()
    assert [r.serial for r in db.query(LoadCellUnit)] == ["0badc0de"]


def test_register_checks_the_mag_ids_shape(db):
    with pytest.raises(ValueError, match="8 MLX90394"):
        LoadCellUnit.register(db, SERIAL, UID, mag_ids=["a"] * 7)
    row, _ = LoadCellUnit.register(db, SERIAL, UID,
                                   mag_ids=["a", 2, None, "d", "e", "f",
                                            "g", "h"])
    assert row.mag_ids[2] is None


# --- the unique constraints --------------------------------------------------

def test_mcu_uid_is_unique(db):
    db.add_all([LoadCellUnit(serial="00000001", mcu_uid=UID),
                LoadCellUnit(serial="00000002", mcu_uid=UID)])
    with pytest.raises(IntegrityError):
        db.commit()


def test_device_axf_id_is_unique(db):
    db.add_all([
        LoadCellUnit(serial="00000001", mcu_uid=UID, device_type_id="1a",
                     device_axf_id="1a.00000001"),
        LoadCellUnit(serial="00000002", mcu_uid=OTHER_UID,
                     device_type_id="1a", device_axf_id="1a.00000001")])
    with pytest.raises(IntegrityError):
        db.commit()


def test_untyped_units_may_all_have_no_device_id(db):
    db.add_all([LoadCellUnit(serial="00000001", mcu_uid=UID),
                LoadCellUnit(serial="00000002", mcu_uid=OTHER_UID)])
    db.commit()
    assert db.query(LoadCellUnit).filter(
        LoadCellUnit.device_axf_id.is_(None)).count() == 2


# --- the type ----------------------------------------------------------------

def _unit(db):
    row, _ = LoadCellUnit.register(db, SERIAL, UID)
    db.commit()
    return row


def _history(db):
    return [(h.previous_type, h.new_type) for h in
            db.query(LoadCellUnitTypeHistory)
            .order_by(LoadCellUnitTypeHistory.id)]


def test_set_type_once(db):
    row = _unit(db)
    assert row.set_type(db, "1A", changed_by="calibrator",
                        model_version=20261106, model_crc32=0xABCD)
    db.commit()
    assert row.device_type_id == "1a"
    assert row.device_axf_id == f"1a.{SERIAL}"
    assert (row.model_version, row.model_crc32) == ("20261106", "0000abcd")
    assert _history(db) == [(None, "1a")]


def test_setting_the_same_type_again_writes_no_history(db):
    row = _unit(db)
    row.set_type(db, "1a", changed_by="calibrator")
    db.commit()
    assert not row.set_type(db, "1a", changed_by="calibrator",
                            model_version="20261201")
    db.commit()
    assert row.model_version == "20261201"
    assert _history(db) == [(None, "1a")]


def test_a_second_type_is_refused_and_writes_nothing(db):
    row = _unit(db)
    row.set_type(db, "1a", changed_by="calibrator", model_version=20261106)
    db.commit()
    with pytest.raises(LoadCellTypeAlreadySet) as e:
        row.set_type(db, "1d", changed_by="calibrator",
                     model_version=20261201)
    assert e.value.current_type == "1a"
    db.commit()
    assert (row.device_type_id, row.model_version) == ("1a", "20261106")
    assert _history(db) == [(None, "1a")]


def test_a_forced_type_needs_a_reason_and_keeps_the_old_one(db):
    row = _unit(db)
    row.set_type(db, "1a", changed_by="calibrator")
    db.commit()
    with pytest.raises(ValueError, match="reason"):
        row.set_type(db, "1d", changed_by="calibrator", force=True)
    assert row.set_type(db, "1d", changed_by="stephen", force=True,
                        reason="pad rework, recalibrated")
    db.commit()
    assert row.device_type_id == "1d"
    assert row.device_axf_id == f"1d.{SERIAL}"
    assert _history(db) == [(None, "1a"), ("1a", "1d")]
    last = db.query(LoadCellUnitTypeHistory).order_by(
        LoadCellUnitTypeHistory.id.desc()).first()
    assert (last.changed_by, last.reason) == ("stephen",
                                              "pad rework, recalibrated")
    out = LoadCellUnitTypeHistoryResponse.model_validate(last)
    assert out.serial == SERIAL


def test_set_type_refuses_what_is_not_a_load_cell_type(db):
    row = _unit(db)
    for bad in (0, "00", "18", None):
        with pytest.raises(ValueError):
            row.set_type(db, bad, changed_by="calibrator")
    with pytest.raises(ValueError, match="changed_by"):
        row.set_type(db, "1a", changed_by="")
    with pytest.raises(ValueError, match="YYYYMMDD"):
        row.set_type(db, "1a", changed_by="x", model_version="2026-11-06")
    db.commit()
    assert row.device_type_id is None and _history(db) == []


def test_update_fields_never_moves_identity(db):
    row = _unit(db)
    row.update_fields(pad_lot="L7", operator="Seth", notes=None)
    assert (row.pad_lot, row.operator) == ("L7", "Seth")
    for field in ("serial", "mcu_uid", "device_type_id", "device_axf_id",
                  "model_version", "model_crc32"):
        with pytest.raises(ValueError, match="not editable"):
            row.update_fields(**{field: "x"})
    assert (row.serial, row.mcu_uid) == (SERIAL, UID)


def test_response_carries_every_column(db):
    row = _unit(db)
    out = LoadCellUnitResponse.model_validate(row)
    columns = {c.name for c in LoadCellUnit.__table__.columns}
    assert columns == set(LoadCellUnitResponse.model_fields)
    assert out.serial == SERIAL and out.device_type_id is None


def test_the_type_names_reach_device_rows():
    from axio_common.models.device import TYPE_ID_NAME_MAP
    for t in LOADCELL_TYPE_IDS:
        assert TYPE_ID_NAME_MAP[t].startswith("Load Cell")


# --- the Tigris family -------------------------------------------------------

def test_loadcell_types_map_to_the_loadcell_family():
    for t in LOADCELL_TYPE_IDS:
        assert TYPE_ID_TO_FAMILY[t] == "loadcell"
    assert family_for_type_id("1A") == "loadcell"
    assert family_for_device_id(f"1a-{SERIAL}") == "loadcell"
    # No procedure yet: nothing expected, rather than the plate list.
    assert default_expected_for("loadcell", 1) == []


@pytest.mark.parametrize("key, expected", [
    (f"loadcell/{SERIAL}/2026-11-07/train/gantry01.csv.gz", "TR-GANTRY01"),
    (f"loadcell/{SERIAL}/2026-11-07/test/press_center-2.csv", "TE-PRESS_CENTER-2"),
    # A capture named in the fleet's way keeps its own marker.
    (f"loadcell/{SERIAL}/2026-11-07/train/{SERIAL}-TR-GNT_11.07.2026.csv.gz",
     "TR-GNT"),
    (f"loadcell/{SERIAL}/2026-11-07/tests.txt", None),
    (f"loadcell/{SERIAL}/models/{SERIAL}-20261107120000-3-f.pack", None),
    (f"loadcell/{SERIAL}/2026-11-07/other/gantry01.csv.gz", None),
    ("loadcell/not-a-serial/2026-11-07/train/gantry01.csv.gz", None),
    # The fleet keys still parse as before.
    ("10/10-00000002/2026-03-23/10-00000002-TR-45V_03.23.2026.csv.gz", "TR-45V"),
])
def test_parse_activity_from_loadcell_keys(key, expected):
    assert parse_activity_from_key(key) == expected
