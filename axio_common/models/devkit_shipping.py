"""Dev-kit bases and shipments: the locked-base inventory (Dyno #1401) and
the boxes that go out (Dyno #1403).

A dev kit is a base board plus a flex, and identity follows the flex: the
`devkits` row is keyed by `18.<flex_fp>` and only remembers the last base it
saw (`mcu_uid`). Before a kit ships, its base is provisioned (BOOT1 fused,
boots from flash) and locked (production keys fused, lifecycle closed), and
its sealed model store gets one record per flex it should run, which can be
for flexes that are not attached. The operator locks bases in batches and
later pulls locked bases from the pile to ship with flexes, so a base needs a
record of its own, keyed by its MCU UID. That is `DevkitBase`.

`DevkitShipment` is one box: who it goes to, the flexes and bases in it, and
its status `ready_to_ship` -> `shipped` (or `cancelled`). The DAQ's ship flow
creates it; the person who packs and labels the box marks it shipped from
axiodash. Force plates have their own orders / deliveries tables; dev kits do
not share them.

Writers, one per fact, none committing (the caller owns the transaction, so
a column change and its audit row land together or not at all):

  - `DevkitBase.report()` is the only writer of a base. Every change appends
    a `DevkitBaseEvent` (`devkit_base_events`), which is never updated or
    deleted.
  - `DevkitShipment.create()`, `.mark_shipped()` and `.cancel()` are the only
    writers of a shipment. Marking one shipped sets `owner_since` to the ship
    date on every flex in it whose current owner is the recipient, through
    `Devkit.set_owner` with the same holder (a date change, so no owner
    history row). DECIDED for #1403 (2026-11-09): the ship date is
    `owner_since`, set when the box is marked shipped.

The MCU UID is spelled only by `devkit.format_mcu_uid` (24 uppercase hex, the
firmware `flexid` / USB-serial word order), never by the load cell's
lowercase helper of the same name.
"""
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    Column, Date, DateTime, ForeignKey, Index, Integer, JSON, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Session, relationship

from axio_common.database import Base
from axio_common.logger import logger
from axio_common.models.devkit import (
    DEVKIT_OWNER_KINDS, DEVKIT_RETURNABLE_KINDS, Devkit,
    device_id_from_flex_fp, flex_fp_from_device_id, format_flex_fp,
    format_mcu_uid, is_devkit_id, unidentified_flex,
)
from axio_common.utils.model_utils import current_time


# --- bases -------------------------------------------------------------------

#: Not yet provisioned.
BASE_STATE_OPEN = "open"
#: BOOT1 fused: boots from flash.
BASE_STATE_PROVISIONED = "provisioned"
#: Production keys fused, lifecycle closed. Shippable from the pile.
BASE_STATE_LOCKED = "locked"
#: Out of service (scrapped, kept as a spare for parts, lost).
BASE_STATE_RETIRED = "retired"
DEVKIT_BASE_STATES = (BASE_STATE_OPEN, BASE_STATE_PROVISIONED,
                      BASE_STATE_LOCKED, BASE_STATE_RETIRED)

#: Fusing is one-way, so a base only moves forward through these; `retired`
#: is reachable from any state and leaves none.
_BASE_STATE_ORDER = {BASE_STATE_OPEN: 0, BASE_STATE_PROVISIONED: 1,
                     BASE_STATE_LOCKED: 2}

BASE_EVENT_CREATED = "created"
BASE_EVENT_PROVISIONED = "provisioned"
BASE_EVENT_LOCKED = "locked"
BASE_EVENT_STATE_CHANGED = "state_changed"
BASE_EVENT_UPDATED = "updated"
BASE_EVENT_STORE_READ = "store_read"
BASE_EVENT_NOTE = "note"
DEVKIT_BASE_EVENT_KINDS = (
    BASE_EVENT_CREATED, BASE_EVENT_PROVISIONED, BASE_EVENT_LOCKED,
    BASE_EVENT_STATE_CHANGED, BASE_EVENT_UPDATED, BASE_EVENT_STORE_READ,
    BASE_EVENT_NOTE,
)

#: The fields a report may set as reported, beside the state, the dates and
#: the store. A None in a report leaves the stored value alone.
_BASE_REPORTED_FIELDS = ("security_state", "firmware_version",
                         "firmware_flavor", "boot_image_version",
                         "app_image_version", "models_image_version")


def canonical_mcu_uid(value) -> str:
    """The base's key: 24 uppercase hex, refusing all zeros (a failed read).

    Raises ValueError for anything else, including None.
    """
    text = format_mcu_uid(value)
    if text is None:
        raise ValueError("mcu_uid is required")
    if set(text) == {"0"}:
        raise ValueError(
            "mcu_uid is all zeros, which is a failed read, not a base")
    return text


def normalize_store_records(records) -> Optional[List[dict]]:
    """The model-store records a base reported, in one spelling.

    None means "not read" and stays None; `[]` is a read of an empty store,
    which is a fact. Each record is `{flex_fp, version, seq}`: `flex_fp` 8
    lowercase hex through `format_flex_fp` (the UNIDENTIFIED sentinel names
    no flex, so a record for it is refused), `version` a string or None,
    `seq` an int or None. Raises ValueError on anything else.
    """
    if records is None:
        return None
    if not isinstance(records, (list, tuple)):
        raise ValueError("store_records must be a list")
    out = []
    for i, rec in enumerate(records):
        if hasattr(rec, "model_dump"):
            rec = rec.model_dump()
        if not isinstance(rec, dict):
            raise ValueError(f"store_records[{i}] is not an object")
        fp = format_flex_fp(rec.get("flex_fp"))
        if fp is None or unidentified_flex(fp):
            raise ValueError(
                f"store_records[{i}] has no flex fingerprint (or the "
                f"UNIDENTIFIED sentinel), so it names no flex")
        version = rec.get("version")
        if version is not None:
            version = str(version).strip() or None
        seq = rec.get("seq")
        if seq is not None and (isinstance(seq, bool)
                                or not isinstance(seq, int)):
            raise ValueError(f"store_records[{i}].seq is not an integer")
        out.append({"flex_fp": fp, "version": version, "seq": seq})
    return out


class DevkitBase(Base):
    """One dev-kit base board, keyed by its STM32 UID."""

    __tablename__ = "devkit_bases"

    #: 24 uppercase hex, `devkit.format_mcu_uid`'s spelling.
    mcu_uid = Column(String, primary_key=True)
    #: See DEVKIT_BASE_STATES.
    state = Column(String, nullable=False, default=BASE_STATE_OPEN,
                   index=True)

    provisioned_at = Column(DateTime(timezone=True), nullable=True)
    provisioned_by = Column(String, nullable=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)
    locked_by = Column(String, nullable=True)

    #: As IDENTIFY reports it.
    security_state = Column(Integer, nullable=True)
    firmware_version = Column(String, nullable=True)
    #: 'internal' | 'customer' as the firmware reports it.
    firmware_flavor = Column(String, nullable=True)
    #: The lockdown's init images.
    boot_image_version = Column(String, nullable=True)
    app_image_version = Column(String, nullable=True)
    models_image_version = Column(String, nullable=True)

    #: The sealed model store as last read from the board: a list of
    #: {flex_fp, version, seq}. NULL = never read; [] = read, empty.
    store_records = Column(JSON, nullable=True)
    store_read_at = Column(DateTime(timezone=True), nullable=True)

    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=current_time)
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=current_time)

    events = relationship(
        "DevkitBaseEvent", back_populates="base",
        cascade="all, delete-orphan", lazy="select",
        order_by="DevkitBaseEvent.id")

    @classmethod
    def report(cls, db: Session, mcu_uid, *,
               state: Optional[str] = None,
               provisioned_at: Optional[datetime] = None,
               locked_at: Optional[datetime] = None,
               security_state: Optional[int] = None,
               firmware_version: Optional[str] = None,
               firmware_flavor: Optional[str] = None,
               boot_image_version: Optional[str] = None,
               app_image_version: Optional[str] = None,
               models_image_version: Optional[str] = None,
               store_records=None,
               store_read_at: Optional[datetime] = None,
               notes: Optional[str] = None,
               by: Optional[str] = None,
               reason: Optional[str] = None,
               ) -> Tuple["DevkitBase", List["DevkitBaseEvent"]]:
        """Create or update a base from what the DAQ saw. The only writer.

        An upsert: a field left None keeps its stored value, so the DAQ can
        report only what it just did (a lock, a store read). Returns the row
        and the events appended, one per kind of change:

          - `created` for a base never seen before;
          - `provisioned` / `locked` when the state reaches one of those
            (with `provisioned_at` / `locked_at` defaulting to now and the
            `_by` columns to `by`), `state_changed` for `retired`;
          - `updated` when a reported field (firmware, flavor, image
            versions, security state) changes, with old and new values;
          - `store_read` whenever `store_records` is sent (a read is an
            observation, so a read that found the same records is recorded
            too, with `changed: false`);
          - `note` when `notes` changes.

        Re-sending the same report writes only the `store_read` event, if it
        carries records.

        The state only moves forward (open -> provisioned -> locked, steps
        may be skipped), because fusing is one-way; any state may move to
        `retired`, and a retired base stays retired. ValueError for an
        unknown state, a backward move, a malformed UID or store record.
        Does NOT commit.
        """
        uid = canonical_mcu_uid(mcu_uid)
        records = normalize_store_records(store_records)
        if state is not None:
            state = str(state).strip().lower()
            if state not in DEVKIT_BASE_STATES:
                raise ValueError(
                    f"state {state!r} is not one of "
                    f"{', '.join(DEVKIT_BASE_STATES)}")
        if security_state is not None and (
                isinstance(security_state, bool)
                or not isinstance(security_state, int)):
            raise ValueError("security_state is an integer")

        now = current_time()
        events: List[DevkitBaseEvent] = []

        def event(kind, detail=None):
            entry = DevkitBaseEvent(mcu_uid=uid, kind=kind, at=now, by=by,
                                    reason=reason, detail=detail)
            events.append(entry)

        row = db.get(cls, uid)
        if row is None:
            row = cls(mcu_uid=uid, state=BASE_STATE_OPEN, created_at=now,
                      updated_at=now)
            db.add(row)
            event(BASE_EVENT_CREATED)

        set_by_move = set()
        if state is not None and state != row.state:
            old = row.state
            if old == BASE_STATE_RETIRED:
                raise ValueError(
                    f"base {uid} is retired; a retired base does not return "
                    f"to {state!r}")
            if (state != BASE_STATE_RETIRED
                    and _BASE_STATE_ORDER[state] < _BASE_STATE_ORDER[old]):
                raise ValueError(
                    f"base {uid} is {old}; it cannot go back to {state!r} "
                    f"(fusing is one-way)")
            row.state = state
            if state == BASE_STATE_PROVISIONED:
                row.provisioned_at = provisioned_at or now
                row.provisioned_by = by
                set_by_move.add("provisioned_at")
                event(BASE_EVENT_PROVISIONED, {"from": old})
            elif state == BASE_STATE_LOCKED:
                row.locked_at = locked_at or now
                row.locked_by = by
                set_by_move.add("locked_at")
                event(BASE_EVENT_LOCKED, {"from": old})
            else:
                event(BASE_EVENT_STATE_CHANGED, {"from": old, "to": state})

        # A date the state move did not set is a correction (or, for a base
        # first seen locked, when it was provisioned), recorded as such.
        changed = {}
        for field, value in (("provisioned_at", provisioned_at),
                             ("locked_at", locked_at)):
            if (field in set_by_move or value is None
                    or value == getattr(row, field)):
                continue
            changed[field] = [_iso(getattr(row, field)), _iso(value)]
            setattr(row, field, value)

        values = {"security_state": security_state,
                  "firmware_version": firmware_version,
                  "firmware_flavor": firmware_flavor,
                  "boot_image_version": boot_image_version,
                  "app_image_version": app_image_version,
                  "models_image_version": models_image_version}
        for field in _BASE_REPORTED_FIELDS:
            value = values[field]
            if isinstance(value, str):
                value = value.strip() or None
            if value is None or value == getattr(row, field):
                continue
            changed[field] = [getattr(row, field), value]
            setattr(row, field, value)
        if changed:
            event(BASE_EVENT_UPDATED, changed)

        if records is not None:
            previous = row.store_records
            row.store_records = records
            row.store_read_at = store_read_at or now
            event(BASE_EVENT_STORE_READ,
                  {"records": records, "previous": previous,
                   "changed": previous != records})

        if notes is not None:
            text = notes.strip() or None
            if text != row.notes:
                row.notes = text
                event(BASE_EVENT_NOTE, {"notes": text})

        if events:
            row.updated_at = now
            for entry in events:
                db.add(entry)
            logger.info(f"Devkit base {uid}: "
                        f"{', '.join(e.kind for e in events)} -> {row.state}")
        return row, events

    def store_flex_fps(self) -> List[str]:
        """The flex fingerprints this base's model store holds a record for."""
        return sorted({r.get("flex_fp") for r in (self.store_records or [])
                       if r.get("flex_fp")})


class DevkitBaseEvent(Base):
    """Append-only history of a base: what changed, who, when, the detail.

    Written by `DevkitBase.report()` in the same transaction as the column
    change. Never updated or deleted.
    """
    __tablename__ = "devkit_base_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mcu_uid = Column(String, ForeignKey("devkit_bases.mcu_uid"),
                     nullable=False, index=True)
    #: See DEVKIT_BASE_EVENT_KINDS.
    kind = Column(String, nullable=False)
    at = Column(DateTime(timezone=True), nullable=False, default=current_time)
    by = Column(String, nullable=True)
    reason = Column(Text, nullable=True)
    detail = Column(JSON, nullable=True)

    base = relationship("DevkitBase", back_populates="events")


Index("ix_devkit_base_events_uid_time",
      DevkitBaseEvent.mcu_uid, DevkitBaseEvent.at)


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


# --- shipments ---------------------------------------------------------------

SHIPMENT_READY = "ready_to_ship"
SHIPMENT_SHIPPED = "shipped"
SHIPMENT_CANCELLED = "cancelled"
DEVKIT_SHIPMENT_STATUSES = (SHIPMENT_READY, SHIPMENT_SHIPPED,
                            SHIPMENT_CANCELLED)

#: A flex on a shipment in one of these cannot go on another. A flex that
#: shipped can ship again (a loan comes back and goes out to someone else);
#: its holdings are in `devkit_owner_history`.
FLEX_HELD_STATUSES = (SHIPMENT_READY,)
#: A base on a shipment in one of these is not in the pile: it is in a box
#: or has left the building. Nothing returns a shipped base to the pile yet.
BASE_HELD_STATUSES = (SHIPMENT_READY, SHIPMENT_SHIPPED)


class DevkitShipmentConflict(Exception):
    """A shipment cannot be created or moved as asked (the server's 409)."""

    def __init__(self, message: str, conflicts: Optional[List[dict]] = None):
        super().__init__(message)
        self.conflicts = conflicts or []


class DevkitShipmentMissing(LookupError):
    """A flex or base the shipment names has no record (the server's 404)."""


class DevkitShipment(Base):
    """One box of dev kits going to one recipient."""

    __tablename__ = "devkit_shipments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: See DEVKIT_SHIPMENT_STATUSES.
    status = Column(String, nullable=False, default=SHIPMENT_READY,
                    index=True)

    # --- recipient ---
    recipient_name = Column(String, nullable=False)
    #: Lowercased. Matched against the flexes' `owner_email` when shipped.
    recipient_email = Column(String, nullable=False, index=True)
    recipient_phone = Column(String, nullable=True)
    recipient_org = Column(String, nullable=True)
    address_line1 = Column(String, nullable=False)
    address_line2 = Column(String, nullable=True)
    city = Column(String, nullable=False)
    #: State / province / region.
    region = Column(String, nullable=True)
    postal_code = Column(String, nullable=True)
    #: ISO 3166-1 alpha-2, uppercase.
    country_code = Column(String(2), nullable=False)

    # --- terms, as `devkits.owner_*` spells them ---
    #: One of DEVKIT_OWNER_KINDS.
    kind = Column(String, nullable=False)
    #: Order / loan / evaluation reference.
    ref = Column(String, nullable=True)
    #: Required for a loan, allowed for an evaluation.
    return_by = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=current_time)
    created_by = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=current_time)

    # --- shipped ---
    carrier = Column(String, nullable=True)
    tracking_number = Column(String, nullable=True)
    shipped_at = Column(DateTime(timezone=True), nullable=True)
    shipped_by = Column(String, nullable=True)

    # --- cancelled ---
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_by = Column(String, nullable=True)
    cancel_reason = Column(Text, nullable=True)

    flexes = relationship(
        "DevkitShipmentFlex", back_populates="shipment",
        cascade="all, delete-orphan", lazy="select",
        order_by="DevkitShipmentFlex.id")
    bases = relationship(
        "DevkitShipmentBase", back_populates="shipment",
        cascade="all, delete-orphan", lazy="select",
        order_by="DevkitShipmentBase.id")

    @classmethod
    def create(cls, db: Session, *, recipient_name: str,
               recipient_email: str, address_line1: str, city: str,
               country_code: str, kind: str,
               flexes: Iterable[str] = (), bases: Iterable[str] = (),
               recipient_phone: Optional[str] = None,
               recipient_org: Optional[str] = None,
               address_line2: Optional[str] = None,
               region: Optional[str] = None,
               postal_code: Optional[str] = None,
               ref: Optional[str] = None,
               return_by: Optional[date] = None,
               notes: Optional[str] = None,
               created_by: Optional[str] = None) -> "DevkitShipment":
        """A new `ready_to_ship` shipment. The only way one is created.

        Checks, in order, with nothing written on a refusal:
          1. the recipient and terms (ValueError): name, an email with `@`,
             address line 1, city, a two-letter country code; `kind` one of
             DEVKIT_OWNER_KINDS, `return_by` required for a loan, allowed
             for an evaluation, refused otherwise; at least one flex or
             base; every flex a dev-kit id that is not the UNIDENTIFIED
             sentinel, every base a UID;
          2. every flex has a `devkits` row and every base a `devkit_bases`
             row (DevkitShipmentMissing, naming each one missing);
          3. every base is `locked` (DevkitShipmentConflict);
          4. no flex is on another `ready_to_ship` shipment, and no base on
             another `ready_to_ship` or `shipped` one
             (DevkitShipmentConflict, naming each and its shipment).
        Duplicates in the lists are collapsed. Does NOT commit.
        """
        fields = _clean_recipient(
            recipient_name=recipient_name, recipient_email=recipient_email,
            recipient_phone=recipient_phone, recipient_org=recipient_org,
            address_line1=address_line1, address_line2=address_line2,
            city=city, region=region, postal_code=postal_code,
            country_code=country_code)
        kind_text = _clean(kind)
        kind_text = kind_text.lower() if kind_text else None
        if kind_text not in DEVKIT_OWNER_KINDS:
            raise ValueError(
                f"kind {kind!r} is not one of {', '.join(DEVKIT_OWNER_KINDS)}")
        if isinstance(return_by, datetime):
            return_by = return_by.date()
        if kind_text == "loan" and return_by is None:
            raise ValueError("return_by is required for a loan")
        if return_by is not None and kind_text not in DEVKIT_RETURNABLE_KINDS:
            raise ValueError(
                f"return_by is only for a kit we expect back "
                f"({' / '.join(DEVKIT_RETURNABLE_KINDS)}), not kind "
                f"{kind_text!r}")

        flex_ids = _unique(_canonical_flex_id(f) for f in flexes or ())
        base_uids = _unique(canonical_mcu_uid(b) for b in bases or ())
        if not flex_ids and not base_uids:
            raise ValueError("a shipment needs at least one flex or base")

        # Row locks (Postgres; SQLite ignores them) so two shipments created
        # at once for the same flex or base cannot both pass the checks below.
        known_flexes = {r.device_axf_id for r in db.query(Devkit)
                        .filter(Devkit.device_axf_id.in_(flex_ids))
                        .with_for_update().all()} if flex_ids else set()
        base_rows = {b.mcu_uid: b for b in db.query(DevkitBase)
                     .filter(DevkitBase.mcu_uid.in_(base_uids))
                     .with_for_update().all()} if base_uids else {}
        missing = ([f for f in flex_ids if f not in known_flexes]
                   + [b for b in base_uids if b not in base_rows])
        if missing:
            raise DevkitShipmentMissing(
                f"no record of {', '.join(missing)}")

        unlocked = [{"mcu_uid": u, "state": base_rows[u].state}
                    for u in base_uids
                    if base_rows[u].state != BASE_STATE_LOCKED]
        if unlocked:
            raise DevkitShipmentConflict(
                "only a locked base can ship: " + ", ".join(
                    f"{c['mcu_uid']} is {c['state']}" for c in unlocked),
                unlocked)

        conflicts = []
        for axf, other in shipments_holding_flexes(
                db, flex_ids, FLEX_HELD_STATUSES).items():
            conflicts.append({"device_axf_id": axf, "shipment_id": other.id,
                              "status": other.status})
        for uid, other in shipments_holding_bases(
                db, base_uids, BASE_HELD_STATUSES).items():
            conflicts.append({"mcu_uid": uid, "shipment_id": other.id,
                              "status": other.status})
        if conflicts:
            raise DevkitShipmentConflict(
                "already on another shipment: " + ", ".join(
                    f"{c.get('device_axf_id') or c.get('mcu_uid')} on "
                    f"#{c['shipment_id']} ({c['status']})"
                    for c in conflicts),
                conflicts)

        now = current_time()
        row = cls(status=SHIPMENT_READY, kind=kind_text, ref=_clean(ref),
                  return_by=return_by, notes=_clean(notes),
                  created_at=now, created_by=_clean(created_by),
                  updated_at=now, **fields)
        row.flexes = [DevkitShipmentFlex(device_axf_id=f) for f in flex_ids]
        row.bases = [DevkitShipmentBase(mcu_uid=b) for b in base_uids]
        db.add(row)
        logger.info(
            f"Devkit shipment for {fields['recipient_email']}: "
            f"{len(flex_ids)} flex(es), {len(base_uids)} base(s)")
        return row

    def mark_shipped(self, db: Session, *, carrier: str,
                     tracking_number: str,
                     shipped_at: Optional[datetime] = None,
                     shipped_by: Optional[str] = None) -> Dict[str, list]:
        """`ready_to_ship` -> `shipped`, and the ship date onto the flexes.

        For every flex in the box whose current owner email is the
        recipient's, `owner_since` becomes `shipped_at`, through
        `Devkit.set_owner` with the same holder and terms, which changes the
        date and writes no owner history. A flex with no owner or another
        owner is left alone and named in the result, so the page can say so.

        Returns `{"owner_since_set": [ids], "owner_not_recipient": [ids]}`.
        DevkitShipmentConflict when the shipment is not ready to ship;
        ValueError without a carrier or tracking number. Does NOT commit.
        """
        if self.status != SHIPMENT_READY:
            raise DevkitShipmentConflict(
                f"shipment #{self.id} is {self.status}; only a "
                f"{SHIPMENT_READY} shipment can be marked shipped")
        carrier = _clean(carrier)
        tracking = _clean(tracking_number)
        if not carrier:
            raise ValueError("carrier is required")
        if not tracking:
            raise ValueError("tracking_number is required")
        when = shipped_at or current_time()
        who = _clean(shipped_by)

        set_ids, other_ids = [], []
        ids = [f.device_axf_id for f in self.flexes]
        kits = {k.device_axf_id: k for k in db.query(Devkit)
                .filter(Devkit.device_axf_id.in_(ids)).all()} if ids else {}
        for axf in ids:
            kit = kits.get(axf)
            if kit is None or (kit.owner_email or "").lower() \
                    != self.recipient_email:
                other_ids.append(axf)
                continue
            kit.set_owner(
                db, owner_uid=kit.owner_uid, owner_email=kit.owner_email,
                owner_org=kit.owner_org, owner_note=kit.owner_note,
                owner_since=when, owner_kind=kit.owner_kind,
                owner_ref=kit.owner_ref,
                owner_return_by=kit.owner_return_by, changed_by=who,
                reason=f"shipped, shipment #{self.id}")
            set_ids.append(axf)

        self.status = SHIPMENT_SHIPPED
        self.carrier = carrier
        self.tracking_number = tracking
        self.shipped_at = when
        self.shipped_by = who
        self.updated_at = current_time()
        logger.info(f"Devkit shipment #{self.id} shipped: {carrier} "
                    f"{tracking}; owner_since set on {len(set_ids)} flex(es)")
        return {"owner_since_set": set_ids, "owner_not_recipient": other_ids}

    def cancel(self, db: Session, *, reason: str,
               cancelled_by: Optional[str] = None) -> None:
        """`ready_to_ship` -> `cancelled`; its flexes and bases are free again.

        DevkitShipmentConflict for a shipment that shipped or was already
        cancelled; ValueError without a reason. Does NOT commit.
        """
        if self.status != SHIPMENT_READY:
            raise DevkitShipmentConflict(
                f"shipment #{self.id} is {self.status}; only a "
                f"{SHIPMENT_READY} shipment can be cancelled")
        text = _clean(reason)
        if not text:
            raise ValueError("a reason is required to cancel a shipment")
        now = current_time()
        self.status = SHIPMENT_CANCELLED
        self.cancelled_at = now
        self.cancelled_by = _clean(cancelled_by)
        self.cancel_reason = text
        self.updated_at = now
        logger.info(f"Devkit shipment #{self.id} cancelled: {text}")


class DevkitShipmentFlex(Base):
    """A flex (a dev kit, `18.<flex_fp>`) in a shipment."""
    __tablename__ = "devkit_shipment_flexes"
    __table_args__ = (
        UniqueConstraint("shipment_id", "device_axf_id",
                         name="uq_devkit_shipment_flexes_shipment_device"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    shipment_id = Column(Integer, ForeignKey("devkit_shipments.id"),
                         nullable=False, index=True)
    device_axf_id = Column(String, ForeignKey("devkits.device_axf_id"),
                           nullable=False, index=True)

    shipment = relationship("DevkitShipment", back_populates="flexes")


class DevkitShipmentBase(Base):
    """A base board in a shipment."""
    __tablename__ = "devkit_shipment_bases"
    __table_args__ = (
        UniqueConstraint("shipment_id", "mcu_uid",
                         name="uq_devkit_shipment_bases_shipment_uid"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    shipment_id = Column(Integer, ForeignKey("devkit_shipments.id"),
                         nullable=False, index=True)
    mcu_uid = Column(String, ForeignKey("devkit_bases.mcu_uid"),
                     nullable=False, index=True)

    shipment = relationship("DevkitShipment", back_populates="bases")


def shipments_holding_flexes(db: Session, device_ids: Iterable[str],
                             statuses: Iterable[str] = FLEX_HELD_STATUSES
                             ) -> Dict[str, DevkitShipment]:
    """{device id: the newest shipment in `statuses` that holds it}."""
    ids = list(device_ids)
    if not ids:
        return {}
    rows = (db.query(DevkitShipmentFlex.device_axf_id, DevkitShipment)
            .join(DevkitShipment,
                  DevkitShipment.id == DevkitShipmentFlex.shipment_id)
            .filter(DevkitShipmentFlex.device_axf_id.in_(ids),
                    DevkitShipment.status.in_(list(statuses)))
            .order_by(DevkitShipment.id.asc()).all())
    return {axf: shipment for axf, shipment in rows}


def shipments_holding_bases(db: Session, mcu_uids: Optional[Iterable[str]],
                            statuses: Iterable[str] = BASE_HELD_STATUSES
                            ) -> Dict[str, DevkitShipment]:
    """{UID: the newest shipment in `statuses` that holds it}; every base
    on such a shipment when `mcu_uids` is None."""
    query = (db.query(DevkitShipmentBase.mcu_uid, DevkitShipment)
             .join(DevkitShipment,
                   DevkitShipment.id == DevkitShipmentBase.shipment_id)
             .filter(DevkitShipment.status.in_(list(statuses))))
    if mcu_uids is not None:
        uids = list(mcu_uids)
        if not uids:
            return {}
        query = query.filter(DevkitShipmentBase.mcu_uid.in_(uids))
    rows = query.order_by(DevkitShipment.id.asc()).all()
    return {uid: shipment for uid, shipment in rows}


def _clean(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _unique(values) -> list:
    out = []
    for v in values:
        if v not in out:
            out.append(v)
    return out


def _canonical_flex_id(value) -> str:
    """`18.<flex_fp>` from a device id in either separator spelling."""
    text = _clean(value)
    if not text or not is_devkit_id(text):
        raise ValueError(f"{value!r} is not a dev-kit device id (18.<flex_fp>)")
    fp = flex_fp_from_device_id(text)
    if fp is None:
        raise ValueError(
            f"{text} is the UNIDENTIFIED flex sentinel, not a device")
    return device_id_from_flex_fp(fp)


def _clean_recipient(**raw) -> dict:
    fields = {k: _clean(v) for k, v in raw.items()}
    for required in ("recipient_name", "recipient_email", "address_line1",
                     "city", "country_code"):
        if not fields[required]:
            raise ValueError(f"{required} is required")
    fields["recipient_email"] = fields["recipient_email"].lower()
    if "@" not in fields["recipient_email"]:
        raise ValueError(
            f"recipient_email {raw['recipient_email']!r} is not an email "
            f"address")
    code = fields["country_code"].upper()
    if len(code) != 2 or not code.isascii() or not code.isalpha():
        raise ValueError(
            f"country_code {raw['country_code']!r} is not an ISO 3166-1 "
            f"alpha-2 code (US, CA, GB, ...)")
    fields["country_code"] = code
    return fields


# --- API shapes --------------------------------------------------------------

class DevkitStoreRecord(BaseModel):
    """One record in a base's sealed model store."""
    flex_fp: str
    version: Optional[str] = None
    seq: Optional[int] = None


class DevkitBaseReportRequest(BaseModel):
    """Body of `PUT /devkit/bases/{mcu_uid}`: what the DAQ just saw.

    Every field is optional; a field left out keeps its stored value. Send
    `store_records: []` for a read of an empty store (omitting it means
    "not read").
    """
    state: Optional[str] = None
    provisioned_at: Optional[datetime] = None
    locked_at: Optional[datetime] = None
    security_state: Optional[int] = None
    firmware_version: Optional[str] = None
    firmware_flavor: Optional[str] = None
    boot_image_version: Optional[str] = None
    app_image_version: Optional[str] = None
    models_image_version: Optional[str] = None
    store_records: Optional[List[DevkitStoreRecord]] = None
    store_read_at: Optional[datetime] = None
    notes: Optional[str] = None
    #: Who did it; defaults to the token's role.
    by: Optional[str] = None
    reason: Optional[str] = None


class DevkitBaseEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    mcu_uid: str
    kind: str
    at: datetime
    by: Optional[str] = None
    reason: Optional[str] = None
    detail: Optional[dict] = None


class DevkitBaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    mcu_uid: str
    state: str
    provisioned_at: Optional[datetime] = None
    provisioned_by: Optional[str] = None
    locked_at: Optional[datetime] = None
    locked_by: Optional[str] = None
    security_state: Optional[int] = None
    firmware_version: Optional[str] = None
    firmware_flavor: Optional[str] = None
    boot_image_version: Optional[str] = None
    app_image_version: Optional[str] = None
    models_image_version: Optional[str] = None
    store_records: Optional[List[DevkitStoreRecord]] = None
    store_read_at: Optional[datetime] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    #: The shipment that holds it (ready_to_ship or shipped), if any.
    shipment_id: Optional[int] = None
    shipment_status: Optional[str] = None
    #: Locked and not on a ready_to_ship or shipped shipment: in the pile.
    available: bool = False
    #: Only on `GET /devkit/bases/{mcu_uid}`, oldest first.
    events: Optional[List[DevkitBaseEventResponse]] = None


class DevkitShipmentRequest(BaseModel):
    """Body of `POST /devkit/shipments`."""
    recipient_name: str
    recipient_email: str
    recipient_phone: Optional[str] = None
    recipient_org: Optional[str] = None
    address_line1: str
    address_line2: Optional[str] = None
    city: str
    region: Optional[str] = None
    postal_code: Optional[str] = None
    country_code: str
    #: One of DEVKIT_OWNER_KINDS.
    kind: str
    ref: Optional[str] = None
    return_by: Optional[date] = None
    notes: Optional[str] = None
    #: Dev-kit device ids (`18.<flex_fp>`).
    flexes: List[str] = []
    #: Base MCU UIDs.
    bases: List[str] = []
    #: Defaults to the token's role.
    created_by: Optional[str] = None


class DevkitShipRequest(BaseModel):
    """Body of `POST /devkit/shipments/{id}/ship`."""
    carrier: str
    tracking_number: str
    #: Defaults to now.
    shipped_at: Optional[datetime] = None
    shipped_by: Optional[str] = None


class DevkitCancelShipmentRequest(BaseModel):
    """Body of `POST /devkit/shipments/{id}/cancel`."""
    reason: str
    cancelled_by: Optional[str] = None


class DevkitShipmentFlexItem(BaseModel):
    """A flex in a shipment, with its unit record's current owner."""
    device_axf_id: str
    flex_fp: Optional[str] = None
    #: The last base the unit record saw.
    mcu_uid: Optional[str] = None
    owner_uid: Optional[str] = None
    owner_email: Optional[str] = None
    owner_org: Optional[str] = None
    owner_since: Optional[datetime] = None
    owner_kind: Optional[str] = None
    owner_ref: Optional[str] = None
    owner_return_by: Optional[date] = None
    #: True when the owner email is the shipment's recipient.
    owner_is_recipient: bool = False
    #: Bases in this shipment whose store holds a record for this flex.
    bases_holding_model: List[str] = []


class DevkitShipmentBaseItem(BaseModel):
    """A base in a shipment, with what its model store holds."""
    mcu_uid: str
    state: Optional[str] = None
    locked_at: Optional[datetime] = None
    firmware_version: Optional[str] = None
    firmware_flavor: Optional[str] = None
    models_image_version: Optional[str] = None
    store_records: Optional[List[DevkitStoreRecord]] = None
    store_read_at: Optional[datetime] = None
    #: Flexes in this shipment its store holds a record for.
    flexes_in_store: List[str] = []


class DevkitShipmentResponse(BaseModel):
    id: int
    status: str
    recipient_name: str
    recipient_email: str
    recipient_phone: Optional[str] = None
    recipient_org: Optional[str] = None
    address_line1: str
    address_line2: Optional[str] = None
    city: str
    region: Optional[str] = None
    postal_code: Optional[str] = None
    country_code: str
    kind: str
    ref: Optional[str] = None
    return_by: Optional[date] = None
    notes: Optional[str] = None
    created_at: datetime
    created_by: Optional[str] = None
    updated_at: datetime
    carrier: Optional[str] = None
    tracking_number: Optional[str] = None
    shipped_at: Optional[datetime] = None
    shipped_by: Optional[str] = None
    cancelled_at: Optional[datetime] = None
    cancelled_by: Optional[str] = None
    cancel_reason: Optional[str] = None
    flexes: List[DevkitShipmentFlexItem] = []
    bases: List[DevkitShipmentBaseItem] = []
    #: Only on the ship call: flexes whose owner_since became the ship date,
    #: and flexes left alone because their owner is not the recipient.
    owner_since_set: Optional[List[str]] = None
    owner_not_recipient: Optional[List[str]] = None


def shipment_response(db: Session, shipment: DevkitShipment
                      ) -> DevkitShipmentResponse:
    """The API shape of a shipment, its flexes' owners and bases' stores."""
    return shipment_responses(db, [shipment])[0]


def shipment_responses(db: Session, shipments: List[DevkitShipment]
                       ) -> List[DevkitShipmentResponse]:
    """`shipment_response` for many, in three queries."""
    flex_ids = {f.device_axf_id for s in shipments for f in s.flexes}
    base_uids = {b.mcu_uid for s in shipments for b in s.bases}
    kits = {k.device_axf_id: k for k in db.query(Devkit)
            .filter(Devkit.device_axf_id.in_(flex_ids)).all()} \
        if flex_ids else {}
    bases = {b.mcu_uid: b for b in db.query(DevkitBase)
             .filter(DevkitBase.mcu_uid.in_(base_uids)).all()} \
        if base_uids else {}

    out = []
    for s in shipments:
        ids = [f.device_axf_id for f in s.flexes]
        uids = [b.mcu_uid for b in s.bases]
        stores = {u: set(bases[u].store_flex_fps()) if u in bases else set()
                  for u in uids}
        flex_items = []
        for axf in ids:
            kit = kits.get(axf)
            fp = axf.split(".", 1)[1]
            item = DevkitShipmentFlexItem(
                device_axf_id=axf, flex_fp=fp,
                bases_holding_model=[u for u in uids if fp in stores[u]])
            if kit is not None:
                item.mcu_uid = kit.mcu_uid
                for f in ("owner_uid", "owner_email", "owner_org",
                          "owner_since", "owner_kind", "owner_ref",
                          "owner_return_by"):
                    setattr(item, f, getattr(kit, f))
                item.owner_is_recipient = (
                    (kit.owner_email or "").lower() == s.recipient_email)
            flex_items.append(item)
        base_items = []
        for u in uids:
            b = bases.get(u)
            item = DevkitShipmentBaseItem(mcu_uid=u)
            if b is not None:
                item = DevkitShipmentBaseItem(
                    mcu_uid=u, state=b.state, locked_at=b.locked_at,
                    firmware_version=b.firmware_version,
                    firmware_flavor=b.firmware_flavor,
                    models_image_version=b.models_image_version,
                    store_records=b.store_records,
                    store_read_at=b.store_read_at,
                    flexes_in_store=[a for a in ids
                                     if a.split(".", 1)[1] in stores[u]])
            base_items.append(item)
        data = {c.name: getattr(s, c.name) for c in s.__table__.columns}
        out.append(DevkitShipmentResponse(**data, flexes=flex_items,
                                          bases=base_items))
    return out
