"""Pure resolution helpers for the calibration procedure registry.

No DB access — operate on already-loaded values so they're trivially
unit-testable and reusable by both the API and the seed.
"""
from __future__ import annotations
from typing import List, Optional


def _blank(v: Optional[str]) -> bool:
    return v is None or v.strip() == ""


def resolve_description(
    base: Optional[str], family: Optional[str], day: Optional[str],
) -> Optional[str]:
    """Most-specific non-blank wins: day -> family -> base."""
    for v in (day, family, base):
        if not _blank(v):
            return v
    return None


def accumulate_tags(
    catalog: Optional[List[str]],
    family: Optional[List[str]],
    day: Optional[List[str]],
) -> List[str]:
    """Concatenate catalog + family + day tags, de-duped, first occurrence kept."""
    out: List[str] = []
    seen: set = set()
    for level in (catalog or [], family or [], day or []):
        for tag in level:
            if tag not in seen:
                seen.add(tag)
                out.append(tag)
    return out


def derive_day_sequence(ordered_member_ids: List[str], reverse: bool) -> List[str]:
    """Given the day's member activity_ids already in master order, return the
    capture sequence — reversed when the day's reverse_order flag is set."""
    return list(reversed(ordered_member_ids)) if reverse else list(ordered_member_ids)


def derive_seed_for_family(by_session: dict) -> dict:
    """Turn {session_number: [activity_id,...]} (from DEFAULT_BY_TYPE_AND_SESSION)
    into a seed plan:
        {
          "order": [activity_id, ...],          # master list order
          "membership": {day_number: [activity_id, ...]},
          "days": {day_number: {"reverse_order": bool}},
        }

    Rules:
      - Master list = day-1 order, with any activity that appears in later days
        but not day 1 appended in its first-seen order.
      - reverse_order[day] = True iff that day's list equals day-1's list reversed
        (compared on the activities common to both, ignoring day-exclusive swaps
        like STK1/2 vs STK3/4).
    """
    days = sorted(by_session.keys())
    day1 = list(by_session.get(days[0], [])) if days else []

    # Day-exclusive activities (appear on a later day but not day 1) — e.g. the
    # STK3/STK4 swapped in for STK1/STK2 on day 2.
    later_exclusive: list = []
    seen = set(day1)
    for d in days[1:]:
        for aid in by_session[d]:
            if aid not in seen:
                seen.add(aid)
                later_exclusive.append(aid)

    # Place day-exclusive activities in their SLOT, not at the end. The slot is
    # right after the day-1-exclusive run (the activities day 1 has that no later
    # day has — the "swapped-out" items, e.g. STK1/STK2). This keeps the swapped
    # pair grouped (…STK1, STK2, STK3, STK4, TIL, PLY) so a reversed day still
    # shows them in the reversed-slot position instead of jumping to the top.
    # Falls back to appending at the end when there's no day-1-exclusive anchor.
    all_later: set = set()
    for d in days[1:]:
        all_later |= set(by_session[d])
    day1_exclusive_idxs = [i for i, a in enumerate(day1) if a not in all_later]
    if later_exclusive and day1_exclusive_idxs:
        insert_at = day1_exclusive_idxs[-1] + 1
        order: list = day1[:insert_at] + later_exclusive + day1[insert_at:]
    else:
        order = list(day1) + later_exclusive

    membership = {d: list(by_session[d]) for d in days}

    day_settings = {}
    for d in days:
        seq = by_session[d]
        if d == days[0]:
            day_settings[d] = {"reverse_order": False}
            continue
        # Compare on common activities (drop day-exclusive ones from both).
        common = [a for a in seq if a in set(day1)]
        common_day1 = [a for a in day1 if a in set(seq)]
        day_settings[d] = {"reverse_order": common == list(reversed(common_day1))}

    return {"order": order, "membership": membership, "days": day_settings}


def validate_family_put(payload: dict, known_activity_ids) -> List[str]:
    """Validate a bulk family-procedure PUT body. Returns a list of human-
    readable error strings (empty list = valid).

    Checks:
      - every activity's activity_id exists in the catalog (known_activity_ids)
      - every day_override.day_number is one of the declared days
    Order-index duplicates are NOT an error — the endpoint reindexes by the
    given order, so callers don't have to pre-normalize.
    """
    errors: List[str] = []
    known = set(known_activity_ids)
    days = {d.get("day_number") for d in (payload.get("days") or [])}

    for a in payload.get("activities") or []:
        aid = a.get("activity_id")
        if aid not in known:
            errors.append(f"Unknown activity_id: {aid!r}")
        for do in a.get("day_overrides") or []:
            dn = do.get("day_number")
            if dn not in days:
                errors.append(
                    f"Activity {aid!r} references undeclared day {dn!r}"
                )
    return errors


def snapshot_for_session(
    *,
    family_activities,   # list[tuple[activity_id, order_index]] in master order
    day_members_by_day,  # dict[int, set[str]]
    days,                # dict[int, dict] with key 'reverse_order'
    day_number,
):
    """Resolve a (family, day) into the ordered activity_id list to snapshot
    on a session row at create time. Members are taken from the day's
    membership set, projected onto master-list order, then reversed if the
    day's reverse_order flag is set. Returns [] when the day isn't defined."""
    day = days.get(day_number)
    if day is None:
        return []
    members = day_members_by_day.get(day_number, set())
    ordered = [aid for aid, _ in family_activities if aid in members]
    if day.get("reverse_order"):
        ordered = list(reversed(ordered))
    return ordered
