"""Dev-kit ownership: `Devkit.set_owner` and `devkit_owner_history` (Dyno #874).

SQLite in memory, only the devkit tables -- no Postgres, no network.

What these pin: a set, a transfer and a clear each write exactly one history
row with the right before/after; re-sending the same holder writes none; and
the UNIDENTIFIED sentinel `18.00000000` can never be given an owner.
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
    Devkit, DevkitAssignmentHistory, DevkitOwnerHistory, EolResult,
)

KIT = "18.a0e0c0a1"
UID_A = "firebaseUidAlice0001"
UID_B = "firebaseUidBob000002"


@pytest.fixture
def db():
    engine = original_create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        Devkit.__table__, DevkitAssignmentHistory.__table__,
        DevkitOwnerHistory.__table__, EolResult.__table__])
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _kit(db, axf=KIT):
    row = Devkit(device_axf_id=axf, device_type_id="18",
                 flex_fp=axf.split(".")[1])
    db.add(row)
    db.flush()
    return row


def _history(db):
    return (db.query(DevkitOwnerHistory)
            .order_by(DevkitOwnerHistory.id.asc()).all())


def test_a_new_unit_has_no_owner(db):
    row = _kit(db)
    db.commit()
    d = row.to_dict()
    for f in ("owner_uid", "owner_email", "owner_org", "owner_since",
              "owner_note"):
        assert d[f] is None


def test_set_writes_columns_and_one_history_row(db):
    row = _kit(db)
    entry = row.set_owner(db, owner_uid=UID_A, owner_email="  Alice@Lab.ORG ",
                          owner_org="Lab A", owner_note="eval unit",
                          changed_by="stephen", reason="shipped")
    db.commit()

    assert row.owner_uid == UID_A
    assert row.owner_email == "alice@lab.org"          # stripped + lowercased
    assert row.owner_org == "Lab A"
    assert row.owner_note == "eval unit"
    assert row.owner_since is not None
    assert entry is not None
    [h] = _history(db)
    assert (h.previous_owner_uid, h.previous_owner_email,
            h.previous_owner_org) == (None, None, None)
    assert (h.new_owner_uid, h.new_owner_email, h.new_owner_org) == (
        UID_A, "alice@lab.org", "Lab A")
    assert h.changed_by == "stephen" and h.reason == "shipped"


def test_transfer_records_previous_and_new(db):
    row = _kit(db)
    row.set_owner(db, owner_uid=UID_A, owner_email="alice@lab.org")
    db.commit()
    first_since = row.owner_since
    row.set_owner(db, owner_uid=UID_B, owner_email="bob@co.com",
                  owner_org="Co", changed_by="ops", reason="transfer")
    db.commit()

    assert row.owner_uid == UID_B
    hist = _history(db)
    assert len(hist) == 2
    assert hist[1].previous_owner_uid == UID_A
    assert hist[1].previous_owner_email == "alice@lab.org"
    assert hist[1].new_owner_uid == UID_B
    assert hist[1].new_owner_org == "Co"
    assert row.owner_since >= first_since


def test_clear_records_previous_and_nulls_everything(db):
    row = _kit(db)
    row.set_owner(db, owner_uid=UID_A, owner_email="alice@lab.org",
                  owner_note="x")
    db.commit()
    entry = row.set_owner(db, changed_by="ops", reason="returned")
    db.commit()

    assert entry is not None
    for f in ("owner_uid", "owner_email", "owner_org", "owner_since",
              "owner_note"):
        assert getattr(row, f) is None
    h = _history(db)[-1]
    assert h.previous_owner_uid == UID_A
    assert (h.new_owner_uid, h.new_owner_email, h.new_owner_org) == (
        None, None, None)


def test_blank_strings_clear_like_none(db):
    row = _kit(db)
    row.set_owner(db, owner_uid=UID_A)
    db.commit()
    row.set_owner(db, owner_uid="  ", owner_email="", owner_org=None)
    db.commit()
    assert row.owner_uid is None
    assert len(_history(db)) == 2


def test_same_holder_again_updates_note_but_writes_no_history(db):
    row = _kit(db)
    row.set_owner(db, owner_uid=UID_A, owner_email="alice@lab.org",
                  owner_note="first")
    db.commit()
    since = row.owner_since
    entry = row.set_owner(db, owner_uid=UID_A, owner_email="ALICE@lab.org",
                          owner_note="second")
    db.commit()

    assert entry is None
    assert len(_history(db)) == 1
    assert row.owner_note == "second"
    assert row.owner_since == since            # same holder, same since


def test_clearing_an_unowned_unit_is_a_no_op(db):
    row = _kit(db)
    assert row.set_owner(db) is None
    db.commit()
    assert _history(db) == []


def test_explicit_owner_since_is_kept(db):
    row = _kit(db)
    when = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    row.set_owner(db, owner_uid=UID_A, owner_since=when)
    db.commit()
    assert row.owner_since.replace(tzinfo=timezone.utc) == when


def test_the_sentinel_can_never_be_owned(db):
    row = _kit(db, "18.00000000")
    with pytest.raises(ValueError, match="sentinel"):
        row.set_owner(db, owner_uid=UID_A)
    assert row.owner_uid is None
    db.rollback()
    assert _history(db) == []


def test_a_malformed_email_is_refused_before_anything_moves(db):
    row = _kit(db)
    with pytest.raises(ValueError, match="email"):
        row.set_owner(db, owner_uid=UID_A, owner_email="not-an-email")
    assert row.owner_uid is None
    assert _history(db) == []
