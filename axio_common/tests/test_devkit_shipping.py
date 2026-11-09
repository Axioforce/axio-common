"""Dev-kit bases and shipments: `DevkitBase.report` and `DevkitShipment`
(Dyno #1401, #1403).

SQLite in memory, only the devkit tables -- no Postgres, no network.

What these pin: a base report writes its columns and one audit event per
kind of change, in the same session; the state only moves forward (fusing is
one-way) and `retired` is final; a shipment refuses unknown flexes and bases,
unlocked bases and anything already in another box, and writes nothing when
it does; marking shipped sets `owner_since` to the ship date only on flexes
held by the recipient, with no owner history row; and a shipped or cancelled
shipment cannot be shipped or cancelled again.
"""
from datetime import date, datetime, timezone

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
    Devkit, DevkitAssignmentHistory, DevkitOwnerHistory,
)
from axio_common.models.devkit_shipping import (
    DevkitBase, DevkitBaseEvent, DevkitShipment, DevkitShipmentBase,
    DevkitShipmentConflict, DevkitShipmentFlex, DevkitShipmentMissing,
    normalize_store_records, shipment_response,
)

UID_A = "0037324E4236501000340020"
UID_B = "0037324E4236501000340021"
UID_C = "0037324E4236501000340022"
KIT_1 = "18.a0e0c0a1"
KIT_2 = "18.a0e0c0a2"
SHIP_AT = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)

RECIPIENT = dict(recipient_name="Ada Lovelace",
                 recipient_email="Ada@Example.COM",
                 recipient_phone="+1 555 0100", recipient_org="Analytical",
                 address_line1="1 Engine Way", city="Chicago", region="IL",
                 postal_code="60601", country_code="us", kind="evaluation",
                 ref="EVAL-7")


@pytest.fixture
def db():
    engine = original_create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        Devkit.__table__, DevkitAssignmentHistory.__table__,
        DevkitOwnerHistory.__table__, DevkitBase.__table__,
        DevkitBaseEvent.__table__, DevkitShipment.__table__,
        DevkitShipmentFlex.__table__, DevkitShipmentBase.__table__])
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _kit(db, axf=KIT_1, owner_email=None):
    row = Devkit(device_axf_id=axf, device_type_id="18",
                 flex_fp=axf.split(".")[1])
    db.add(row)
    db.flush()
    if owner_email:
        row.set_owner(db, owner_email=owner_email, owner_kind="evaluation",
                      owner_ref="EVAL-7", owner_note="trade show")
    db.commit()
    return row


def _locked(db, uid, records=None):
    row, _ = DevkitBase.report(db, uid, state="locked", by="stephen",
                               store_records=records)
    db.commit()
    return row


def _events(db, uid):
    return [e.kind for e in db.query(DevkitBaseEvent)
            .filter(DevkitBaseEvent.mcu_uid == uid)
            .order_by(DevkitBaseEvent.id).all()]


# --- bases --------------------------------------------------------------------

def test_first_report_creates_the_base_and_its_events(db):
    row, events = DevkitBase.report(
        db, UID_A.lower(), state="provisioned", security_state=3,
        firmware_version="0.9.53", firmware_flavor="customer", by="stephen")
    db.commit()
    assert row.mcu_uid == UID_A                     # stored canonical
    assert row.state == "provisioned"
    assert row.provisioned_by == "stephen" and row.provisioned_at
    assert row.locked_at is None
    assert [e.kind for e in events] == ["created", "provisioned", "updated"]
    assert _events(db, UID_A) == ["created", "provisioned", "updated"]
    assert events[2].detail["firmware_version"] == [None, "0.9.53"]


def test_the_same_report_again_writes_nothing(db):
    DevkitBase.report(db, UID_A, state="locked", firmware_version="0.9.53")
    db.commit()
    _, events = DevkitBase.report(db, UID_A, state="locked",
                                  firmware_version="0.9.53")
    db.commit()
    assert events == []
    assert _events(db, UID_A) == ["created", "locked", "updated"]


def test_lock_then_store_read(db):
    _locked(db, UID_A)
    records = [{"flex_fp": "A0E0C0A1", "version": "1.26.1029+0", "seq": 1},
               {"flex_fp": 0xa0e0c0a2, "version": "1.26.1029+0", "seq": 2}]
    row, events = DevkitBase.report(db, UID_A, store_records=records)
    db.commit()
    assert [e.kind for e in events] == ["store_read"]
    assert row.store_records[0]["flex_fp"] == "a0e0c0a1"
    assert row.store_records[1]["flex_fp"] == "a0e0c0a2"
    assert row.store_read_at is not None
    assert events[0].detail["changed"] is True
    # A re-read is an observation and is recorded, even with no change.
    _, again = DevkitBase.report(db, UID_A, store_records=records)
    assert again[0].detail["changed"] is False
    assert row.store_flex_fps() == ["a0e0c0a1", "a0e0c0a2"]


def test_an_empty_store_read_is_not_a_missing_one(db):
    row, _ = DevkitBase.report(db, UID_A, store_records=[])
    assert row.store_records == [] and row.store_read_at is not None
    row2, _ = DevkitBase.report(db, UID_B)
    assert row2.store_records is None and row2.store_read_at is None


def test_the_state_never_goes_back(db):
    _locked(db, UID_A)
    for back in ("provisioned", "open"):
        with pytest.raises(ValueError, match="one-way"):
            DevkitBase.report(db, UID_A, state=back)


def test_retired_is_final(db):
    _locked(db, UID_A)
    row, events = DevkitBase.report(db, UID_A, state="retired",
                                    reason="cracked")
    db.commit()
    assert row.state == "retired"
    assert events[0].kind == "state_changed"
    assert events[0].detail == {"from": "locked", "to": "retired"}
    assert events[0].reason == "cracked"
    with pytest.raises(ValueError, match="retired"):
        DevkitBase.report(db, UID_A, state="locked")


def test_a_base_first_seen_locked_may_say_when_it_was_provisioned(db):
    when = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    row, events = DevkitBase.report(db, UID_A, state="locked",
                                    provisioned_at=when)
    assert row.provisioned_at == when and row.locked_at is not None
    assert [e.kind for e in events] == ["created", "locked", "updated"]


@pytest.mark.parametrize("bad", [
    "00000000000000000000000",            # 23 digits
    "000000000000000000000000",           # a failed read
    "zz37324E4236501000340020",
    None,
])
def test_a_bad_uid_is_refused(db, bad):
    with pytest.raises(ValueError):
        DevkitBase.report(db, bad)


def test_bad_state_and_records_are_refused(db):
    with pytest.raises(ValueError, match="state"):
        DevkitBase.report(db, UID_A, state="shipped")
    with pytest.raises(ValueError, match="sentinel"):
        normalize_store_records([{"flex_fp": "00000000"}])
    with pytest.raises(ValueError, match="seq"):
        normalize_store_records([{"flex_fp": "a0e0c0a1", "seq": "3"}])


def test_notes_change_is_an_event(db):
    _, events = DevkitBase.report(db, UID_A, notes="trade show batch")
    assert [e.kind for e in events] == ["created", "note"]
    _, events = DevkitBase.report(db, UID_A, notes="trade show batch")
    assert events == []


# --- shipments ----------------------------------------------------------------

def _ship(db, flexes=(KIT_1,), bases=(UID_A,), **over):
    fields = {**RECIPIENT, **over}
    row = DevkitShipment.create(db, flexes=flexes, bases=bases,
                                created_by="stephen", **fields)
    db.commit()
    return row


def test_create_a_shipment(db):
    _kit(db)
    _locked(db, UID_A, [{"flex_fp": "a0e0c0a1", "version": "1", "seq": 1}])
    row = _ship(db, flexes=["18-A0E0C0A1"], bases=[UID_A.lower()])
    assert row.status == "ready_to_ship"
    assert row.recipient_email == "ada@example.com"
    assert row.country_code == "US"
    assert [f.device_axf_id for f in row.flexes] == [KIT_1]
    assert [b.mcu_uid for b in row.bases] == [UID_A]
    resp = shipment_response(db, row)
    assert resp.flexes[0].bases_holding_model == [UID_A]
    assert resp.bases[0].flexes_in_store == [KIT_1]
    assert resp.bases[0].store_records[0].flex_fp == "a0e0c0a1"


@pytest.mark.parametrize("over, match", [
    ({"recipient_email": "nobody"}, "email"),
    ({"country_code": "USA"}, "country_code"),
    ({"city": "  "}, "city"),
    ({"kind": "gift"}, "kind"),
    ({"kind": "loan"}, "return_by"),
    ({"kind": "customer", "return_by": date(2027, 1, 1)}, "return_by"),
])
def test_bad_recipient_or_terms_are_refused(db, over, match):
    _kit(db)
    _locked(db, UID_A)
    with pytest.raises(ValueError, match=match):
        _ship(db, **over)
    db.rollback()
    assert db.query(DevkitShipment).count() == 0


def test_an_empty_shipment_and_the_sentinel_are_refused(db):
    with pytest.raises(ValueError, match="at least one"):
        _ship(db, flexes=[], bases=[])
    with pytest.raises(ValueError, match="sentinel"):
        _ship(db, flexes=["18.00000000"], bases=[])


def test_unknown_flex_or_base_is_missing(db):
    _kit(db)
    with pytest.raises(DevkitShipmentMissing) as e:
        _ship(db, flexes=[KIT_1, KIT_2], bases=[UID_B])
    assert KIT_2 in str(e.value) and UID_B in str(e.value)
    assert KIT_1 not in str(e.value)


def test_an_unlocked_base_cannot_ship(db):
    _kit(db)
    DevkitBase.report(db, UID_A, state="provisioned")
    db.commit()
    with pytest.raises(DevkitShipmentConflict, match="locked") as e:
        _ship(db)
    assert e.value.conflicts == [{"mcu_uid": UID_A, "state": "provisioned"}]


def test_nothing_goes_in_two_boxes(db):
    _kit(db)
    _kit(db, KIT_2)
    _locked(db, UID_A)
    _locked(db, UID_B)
    first = _ship(db, flexes=[KIT_1], bases=[UID_A])
    with pytest.raises(DevkitShipmentConflict) as e:
        _ship(db, flexes=[KIT_1, KIT_2], bases=[UID_A, UID_B])
    assert {(c.get("device_axf_id") or c.get("mcu_uid"), c["shipment_id"])
            for c in e.value.conflicts} == {(KIT_1, first.id),
                                            (UID_A, first.id)}
    db.rollback()
    assert db.query(DevkitShipment).count() == 1


def test_cancel_frees_the_contents(db):
    _kit(db)
    _locked(db, UID_A)
    first = _ship(db)
    first.cancel(db, reason="wrong address", cancelled_by="stephen")
    db.commit()
    assert first.status == "cancelled" and first.cancel_reason
    second = _ship(db)
    assert second.status == "ready_to_ship"


def test_a_shipped_base_stays_out_of_the_pile_a_shipped_flex_does_not(db):
    _kit(db)
    _locked(db, UID_A)
    _locked(db, UID_B)
    first = _ship(db, bases=[UID_A])
    first.mark_shipped(db, carrier="FedEx", tracking_number="7946")
    db.commit()
    with pytest.raises(DevkitShipmentConflict, match=UID_A):
        _ship(db, bases=[UID_A])
    db.rollback()
    again = _ship(db, bases=[UID_B])            # the flex itself may go again
    assert again.status == "ready_to_ship"


def test_ship_sets_owner_since_only_for_the_recipient(db):
    held = _kit(db, KIT_1, owner_email="ada@example.com")
    other = _kit(db, KIT_2, owner_email="bob@example.com")
    _locked(db, UID_A)
    history_before = db.query(DevkitOwnerHistory).count()
    other_since = other.owner_since
    row = _ship(db, flexes=[KIT_1, KIT_2], bases=[UID_A])
    result = row.mark_shipped(db, carrier=" FedEx ", tracking_number=" 7946 ",
                              shipped_at=SHIP_AT, shipped_by="pat")
    db.commit()
    assert result == {"owner_since_set": [KIT_1],
                      "owner_not_recipient": [KIT_2]}
    assert row.status == "shipped" and row.carrier == "FedEx"
    assert row.tracking_number == "7946" and row.shipped_by == "pat"
    db.refresh(held)
    db.refresh(other)
    assert held.owner_since.replace(tzinfo=timezone.utc) == SHIP_AT
    # The holder and terms survive the date change.
    assert (held.owner_email, held.owner_kind, held.owner_ref,
            held.owner_note) == ("ada@example.com", "evaluation", "EVAL-7",
                                 "trade show")
    assert other.owner_since == other_since
    assert db.query(DevkitOwnerHistory).count() == history_before


def test_ship_twice_or_after_cancel_is_a_conflict(db):
    _kit(db)
    _locked(db, UID_A)
    row = _ship(db)
    with pytest.raises(ValueError, match="tracking"):
        row.mark_shipped(db, carrier="FedEx", tracking_number=" ")
    row.mark_shipped(db, carrier="FedEx", tracking_number="7946")
    db.commit()
    with pytest.raises(DevkitShipmentConflict, match="shipped"):
        row.mark_shipped(db, carrier="FedEx", tracking_number="7946")
    with pytest.raises(DevkitShipmentConflict, match="shipped"):
        row.cancel(db, reason="too late")


def test_cancel_needs_a_reason(db):
    _kit(db)
    _locked(db, UID_A)
    row = _ship(db)
    with pytest.raises(ValueError, match="reason"):
        row.cancel(db, reason="")
