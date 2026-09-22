"""Dev-kit board identity: uid spellings, `Devkit.record_identity`, and the
automatic-EOL rule in `shippability()` (WI-546 / WI-547).

SQLite in memory, only the three devkit tables -- no Postgres, no network.

The two traps these pin:

  - **mcu_uid byte order.** The firmware's text reply prints the STM32 UID
    words w2, w1, w0 big-endian; the binary IDENTIFY reply carries them
    little-endian w0, w1, w2. One board, two strings. The canonical spelling
    is the text / USB-serial one, and raw IDENTIFY bytes convert to it
    exactly as the SDK's `eol.mcu_uid_str` does.
  - **all zeros is a failed read, not an id**, for both uids -- the same
    shape of mistake as storing the flex sentinel as a board.
"""
from datetime import datetime, timezone

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

from sqlalchemy.orm import sessionmaker

from axio_common.database import Base
from axio_common.models.devkit import (
    AUTO_EOL_TOOL, Devkit, DevkitAssignmentHistory, EolResult,
    format_mcu_uid, format_tmp_uid, shippability,
)

KIT = "18.a0e0c001"
TMP = "21E8A3E64CCF"
MCU = "0037324E4236501000340035"          # flexid / USB-serial order


@pytest.fixture
def db():
    engine = original_create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        Devkit.__table__, DevkitAssignmentHistory.__table__,
        EolResult.__table__])
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


# --- spellings ---------------------------------------------------------------

def test_tmp_uid_is_twelve_uppercase_hex_from_str_or_bytes():
    assert format_tmp_uid("21e8a3e64ccf") == TMP
    assert format_tmp_uid(bytes.fromhex("21e8a3e64ccf")) == TMP
    assert format_tmp_uid(None) is None
    for bad in ("21E8A3E64CC", "21E8A3E64CCFG", "", b"\x00" * 5):
        with pytest.raises(ValueError):
            format_tmp_uid(bad)


def test_mcu_uid_bytes_convert_to_the_flexid_order_not_their_hex():
    # IDENTIFY carries w0, w1, w2 little-endian; the board ledger, the
    # firmware's text reply and the USB serial print w2 w1 w0 big-endian.
    w2, w1, w0 = 0x0037324E, 0x42365010, 0x00340035
    raw = (w0.to_bytes(4, "little") + w1.to_bytes(4, "little")
           + w2.to_bytes(4, "little"))
    assert format_mcu_uid(raw) == MCU
    # The trap: the raw bytes' own hex is the full reversal, a different
    # string for the same board.
    assert raw.hex().upper() != MCU
    assert raw.hex().upper() == bytes.fromhex(MCU)[::-1].hex().upper()


def test_mcu_uid_string_is_taken_as_given_order_and_uppercased():
    assert format_mcu_uid(MCU.lower()) == MCU
    with pytest.raises(ValueError):
        format_mcu_uid(MCU[:-1])


# --- record_identity ---------------------------------------------------------

def test_first_report_creates_the_row_with_both_uids(db):
    row = Devkit.record_identity(db, "18-A0E0C001", tmp_uid=TMP.lower(),
                                 mcu_uid=MCU, source="test")
    db.commit()
    assert row.device_axf_id == KIT              # dashed/upper normalised
    assert (row.flex_fp, row.tmp_uid, row.mcu_uid) == ("a0e0c001", TMP, MCU)
    # First sight of an identifier is not a swap.
    assert db.query(DevkitAssignmentHistory).count() == 0


def test_a_changed_base_board_is_an_audited_swap(db):
    Devkit.record_identity(db, KIT, tmp_uid=TMP, mcu_uid=MCU, source="t")
    db.commit()
    other = "0037324E4236501000340099"
    Devkit.record_identity(db, KIT, mcu_uid=other, source="t2")
    db.commit()
    row = db.get(Devkit, KIT)
    assert row.mcu_uid == other
    assert row.tmp_uid == TMP                      # None erased nothing
    swaps = db.query(DevkitAssignmentHistory).all()
    assert [(s.which, s.old_value, s.new_value) for s in swaps] == [
        ("base", MCU, other)]


def test_only_type_18_is_touched(db):
    assert Devkit.record_identity(db, "10.a0e0c001", tmp_uid=TMP,
                                  mcu_uid=MCU, source="t") is None
    assert db.query(Devkit).count() == 0


def test_the_unidentified_sentinel_is_refused(db):
    assert Devkit.record_identity(db, "18.00000000", tmp_uid=TMP,
                                  source="t") is None
    assert db.query(Devkit).count() == 0


def test_all_zero_uids_are_failed_reads_and_are_not_stored(db):
    row = Devkit.record_identity(db, KIT, tmp_uid="000000000000",
                                 mcu_uid="0" * 24, source="t")
    db.commit()
    assert row.tmp_uid is None and row.mcu_uid is None
    assert row.flex_fp == "a0e0c001"


def test_a_fingerprint_from_another_unit_is_refused(db):
    with pytest.raises(ValueError):
        Devkit.record_identity(db, KIT, flex_fp="5fdb3916", tmp_uid=TMP,
                               source="t")


def test_a_malformed_uid_is_refused_before_anything_is_written(db):
    with pytest.raises(ValueError):
        Devkit.record_identity(db, KIT, mcu_uid="53494d554c4154", source="t")
    db.commit()
    assert db.query(Devkit).count() == 0


# --- shippability vs the automatic row --------------------------------------

def _eol(db, *, tool, passed, checks, mcu=MCU):
    row = EolResult(device_axf_id=KIT, flex_fp="a0e0c001", tmp_uid=TMP,
                    mcu_uid=mcu, passed=passed, simulated=False,
                    checks=checks, tool=tool,
                    completed_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
                    recorded_at=datetime(2026, 9, 22, tzinfo=timezone.utc))
    db.add(row)
    db.commit()
    return row


def test_an_automatic_row_alone_never_ships_and_says_what_it_is(db):
    Devkit.record_identity(db, KIT, tmp_uid=TMP, mcu_uid=MCU, source="t")
    _eol(db, tool=AUTO_EOL_TOOL, passed=False,
         checks={"known_load": {"passed": True, "detail": "PASSED"}})
    out = shippability(db, KIT)
    assert out["shippable"] is False
    assert "Only automatic" in out["reason"]
    assert "passed" in out["reason"]
    assert "identity, mags, imu, temp, usb, rate" in out["reason"]


def test_an_automatic_row_is_excluded_even_if_marked_passed(db):
    # Belt and braces: nothing should store one as passed, but if anything
    # ever did, it still must not be the evidence a unit ships on.
    _eol(db, tool=AUTO_EOL_TOOL, passed=True,
         checks={"known_load": {"passed": True, "detail": "PASSED"}})
    assert shippability(db, KIT)["shippable"] is False


def test_a_bench_pass_still_ships_next_to_an_automatic_row(db):
    Devkit.record_identity(db, KIT, tmp_uid=TMP, mcu_uid=MCU, source="t")
    _eol(db, tool=AUTO_EOL_TOOL, passed=False,
         checks={"known_load": {"passed": True, "detail": "PASSED"}})
    _eol(db, tool="axio-devkit eol", passed=True, checks={})
    assert shippability(db, KIT)["shippable"] is True


def test_uid_case_is_not_a_board_swap(db):
    Devkit.record_identity(db, KIT, tmp_uid=TMP, mcu_uid=MCU, source="t")
    _eol(db, tool="axio-devkit eol", passed=True, checks={}, mcu=MCU.lower())
    assert shippability(db, KIT)["shippable"] is True


def test_a_real_swap_still_un_ships(db):
    Devkit.record_identity(db, KIT, tmp_uid=TMP, mcu_uid=MCU, source="t")
    _eol(db, tool="axio-devkit eol", passed=True, checks={},
         mcu="0037324E4236501000340099")
    out = shippability(db, KIT)
    assert out["shippable"] is False and "mcu_uid" in out["reason"]
