"""Dev-kit owner kind / ref / return-by (Dyno #885, firmware SHIPPING.md §4).

SQLite in memory, only the devkit tables -- no Postgres, no network.

What these pin: the kind is validated against DEVKIT_OWNER_KINDS; a loan needs
a return date and only loans / evaluations may carry one; a change of kind for
the same person is a change of holding and writes history; changing only the
ref or return date does not; clearing the owner clears all three; and an
unshippable kit is NOT refused (the gate is advisory).
"""
from datetime import date, datetime

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
    DEVKIT_OWNER_KINDS, Devkit, DevkitAssignmentHistory, DevkitOwnerHistory,
    DevkitOwnerHistoryResponse, DevkitResponse, EolResult, shippability,
)

KIT = "18.b0e0c0a2"
EMAIL = "jane@lab.edu"


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
                 flex_fp=axf.split(".")[1],
                 initialized_at=datetime(2026, 10, 1),
                 updated_at=datetime(2026, 10, 1))
    db.add(row)
    db.flush()
    return row


def _history(db):
    return (db.query(DevkitOwnerHistory)
            .order_by(DevkitOwnerHistory.id.asc()).all())


def test_kinds_are_the_four_in_the_protocol():
    assert DEVKIT_OWNER_KINDS == ("customer", "loan", "evaluation",
                                  "internal")


def test_kind_ref_and_return_by_are_stored_and_in_history(db):
    row = _kit(db)
    row.set_owner(db, owner_email=EMAIL, owner_org="Example Lab",
                  owner_kind=" Evaluation ", owner_ref="EVAL-2026-003",
                  owner_return_by=date(2026, 12, 31), reason="shipped")
    db.commit()

    assert row.owner_kind == "evaluation"       # stripped + lowercased
    assert row.owner_ref == "EVAL-2026-003"
    assert row.owner_return_by == date(2026, 12, 31)
    [h] = _history(db)
    assert h.previous_owner_kind is None
    assert (h.new_owner_kind, h.new_owner_ref, h.new_owner_return_by) == (
        "evaluation", "EVAL-2026-003", date(2026, 12, 31))
    d = row.to_dict()
    assert d["owner_kind"] == "evaluation"
    assert d["owner_return_by"] == date(2026, 12, 31)
    resp = DevkitResponse.model_validate(row)
    assert resp.owner_ref == "EVAL-2026-003"
    hresp = DevkitOwnerHistoryResponse.model_validate(h)
    assert hresp.new_owner_kind == "evaluation"


@pytest.mark.parametrize("bad", ["sold", "LOANER", "external"])
def test_an_unknown_kind_is_refused_before_anything_moves(db, bad):
    row = _kit(db)
    with pytest.raises(ValueError, match="owner_kind"):
        row.set_owner(db, owner_email=EMAIL, owner_kind=bad)
    assert row.owner_email is None
    assert _history(db) == []


def test_a_loan_needs_a_return_date(db):
    row = _kit(db)
    with pytest.raises(ValueError, match="required for a loan"):
        row.set_owner(db, owner_email=EMAIL, owner_kind="loan")
    assert row.owner_email is None
    row.set_owner(db, owner_email=EMAIL, owner_kind="loan",
                  owner_return_by=date(2026, 11, 1))
    db.commit()
    assert row.owner_kind == "loan"


def test_an_evaluation_may_omit_the_return_date(db):
    row = _kit(db)
    row.set_owner(db, owner_email=EMAIL, owner_kind="evaluation")
    db.commit()
    assert row.owner_return_by is None


@pytest.mark.parametrize("kind", ["customer", "internal", None])
def test_return_by_is_refused_for_a_kit_not_expected_back(db, kind):
    row = _kit(db)
    with pytest.raises(ValueError, match="owner_return_by"):
        row.set_owner(db, owner_email=EMAIL, owner_kind=kind,
                      owner_return_by=date(2026, 11, 1))
    assert row.owner_email is None


def test_kind_without_a_holder_is_refused_not_dropped(db):
    row = _kit(db)
    with pytest.raises(ValueError, match="need a holder"):
        row.set_owner(db, owner_kind="customer", owner_ref="SO-1")


def test_evaluation_converting_to_customer_is_a_holder_change(db):
    row = _kit(db)
    row.set_owner(db, owner_email=EMAIL, owner_kind="evaluation",
                  owner_ref="EVAL-1", owner_return_by=date(2026, 12, 31))
    db.commit()
    entry = row.set_owner(db, owner_email=EMAIL, owner_kind="customer",
                          owner_ref="SO-42", reason="converted: SO-42")
    db.commit()

    assert entry is not None
    hist = _history(db)
    assert len(hist) == 2
    assert hist[1].previous_owner_email == hist[1].new_owner_email == EMAIL
    assert (hist[1].previous_owner_kind, hist[1].new_owner_kind) == (
        "evaluation", "customer")
    assert hist[1].new_owner_ref == "SO-42"
    assert hist[1].new_owner_return_by is None
    assert row.owner_return_by is None          # whole-state: cleared


def test_legacy_null_kind_gaining_a_kind_is_a_change(db):
    row = _kit(db)
    row.set_owner(db, owner_email=EMAIL)                 # pre-#885 shape
    db.commit()
    assert row.set_owner(db, owner_email=EMAIL,
                         owner_kind="customer") is not None
    db.commit()
    assert len(_history(db)) == 2


def test_extending_a_loan_updates_terms_but_writes_no_history(db):
    row = _kit(db)
    row.set_owner(db, owner_email=EMAIL, owner_kind="loan",
                  owner_ref="LOAN-7", owner_return_by=date(2026, 11, 1))
    db.commit()
    entry = row.set_owner(db, owner_email=EMAIL, owner_kind="loan",
                          owner_ref="LOAN-7b",
                          owner_return_by=date(2027, 1, 15))
    db.commit()
    assert entry is None
    assert len(_history(db)) == 1
    assert row.owner_ref == "LOAN-7b"
    assert row.owner_return_by == date(2027, 1, 15)


def test_clearing_clears_kind_ref_and_return_by(db):
    row = _kit(db)
    row.set_owner(db, owner_email=EMAIL, owner_kind="loan", owner_ref="L-1",
                  owner_return_by=date(2026, 11, 1))
    db.commit()
    row.set_owner(db, reason="returned: L-1")
    db.commit()
    for f in ("owner_kind", "owner_ref", "owner_return_by"):
        assert getattr(row, f) is None
    h = _history(db)[-1]
    assert h.previous_owner_kind == "loan"
    assert (h.new_owner_kind, h.new_owner_ref, h.new_owner_return_by) == (
        None, None, None)


def test_an_unshippable_kit_is_not_refused(db):
    """The gate is advisory (Stephen, 2026-10-12): no EOL on file, owner
    still recorded."""
    row = _kit(db)
    assert shippability(db, KIT)["shippable"] is False
    row.set_owner(db, owner_email=EMAIL, owner_kind="customer",
                  owner_ref="SO-1")
    db.commit()
    assert row.owner_kind == "customer"
