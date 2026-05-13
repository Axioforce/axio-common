"""
Canonical calibration-activity registry.

Lives in axio-common so the DAQ, axio-server, and any analysis tool all
agree on what activities exist, what each one means, and the expected
set per (plate_family, session_number) for a complete calibration day.

Plate families are the calibration-procedure groupings:
    "lite" — type ids 06, 10
    "lp"   — type ids 07, 11 (Launchpad)
    "xl"   — type ids 08, 12

The expected set for a session is resolved as:
    1. If <bucket>/_config/session_expected_activities.json has an entry for
       (plate_family, session_number), use it.
    2. Otherwise fall back to DEFAULT_BY_TYPE_AND_SESSION[plate_family][N].
    3. As a last resort fall back to DEFAULT_EXPECTED (all activities).

axio-server owns the bucket-side override; this module just owns the
defaults and helpers.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# Activity catalog split by kind. Joined form is "TR-<id>" / "TE-<id>",
# matching the filename pattern used everywhere else.
ACTIVITIES: Dict[str, Dict[str, str]] = {
    "train": {
        "BER":  "Flat on Bertec — hand-push, step, stand, bounce",
        "45V":  "Vertical 45 lb dumbbell, 6×5 grid (use the foam-spacer setup)",
        "HOP":  "Big hop",
        "90S":  "Stand holding 90 pounds",
        "SLH":  "Single-leg hops",
        "LUN":  "Lunges",
        "STA":  "Wide-stance stand (1 minute)",
        "HBW":  "Half body weight, 5×4 grid each side",
        "STK1": "Sticky-note set 1 — hand-push, step, stand, bounce",
        "STK2": "Sticky-note set 2 — hand-push, step, stand, bounce",
        "STK3": "Sticky-note set 3 — hand-push, step, stand, bounce",
        "STK4": "Sticky-note set 4 — hand-push, step, stand, bounce",
        "TIL":  "Tile under feet — hand-push, step, stand, bounce",
        "PLY":  "Plywood under feet — hand-push, step, stand, bounce (stand on plate after setup to settle)",
        "LBJ":  "LBJ — dynamic-load capture (used in LP and XL procedures)",
    },
    "test": {
        "45V": "45 lb dumbbell vertical, 5×4 grid, snake from front-left, 30s total (lift the dumbbell, don't slide it)",
        "HBW": "Half body weight, 4×4 grid, 45s each side, two partial-bodyweight levels per position",
        "OLS": "One-leg stand — 4 corners + center, facing 8 directions, 30s",
        "TLS": "Two-leg stand — both heels in corners + center, facing 8 directions, 30s",
        "HOP": "Hop covering full plate in 4 directions, 30s",
    },
}

# Inverse lookup: "TR-BER" -> "Flat on Bertec — ..."
_FLAT: Dict[str, str] = {}
for _kind, _activities in ACTIVITIES.items():
    _prefix = "TR-" if _kind == "train" else "TE-"
    for _code, _desc in _activities.items():
        _FLAT[f"{_prefix}{_code}"] = _desc


def activity_description(activity_id: str) -> str:
    """'TR-BER' -> human-readable description; '' if unknown."""
    return _FLAT.get(activity_id, "")


# Default expected set when no plate-family default applies. All activities
# in order: trains first, then tests. Includes LBJ.
DEFAULT_EXPECTED: List[str] = (
    [f"TR-{code}" for code in ACTIVITIES["train"].keys()]
    + [f"TE-{code}" for code in ACTIVITIES["test"].keys()]
)


# Device type id (first segment of device_id, e.g. "10" of "10-00000002") to
# plate-family name. Added type ids only need to be mapped here.
TYPE_ID_TO_FAMILY: Dict[str, str] = {
    "06": "lite",
    "10": "lite",
    "07": "lp",
    "11": "lp",
    "08": "xl",
    "12": "xl",
}


def family_for_type_id(type_id: str) -> Optional[str]:
    """'10' -> 'lite'. None when no mapping exists."""
    return TYPE_ID_TO_FAMILY.get((type_id or "").lower())


def family_for_device_id(device_id: str) -> Optional[str]:
    """'10-00000002' -> 'lite'. None when no mapping exists."""
    if not device_id or "-" not in device_id:
        return None
    return family_for_type_id(device_id.split("-", 1)[0])


# Per-(plate_family, session_number) calibration procedures. Order matters —
# it's the operator's intended capture sequence and the UI shows activities
# in this order.
DEFAULT_BY_TYPE_AND_SESSION: Dict[str, Dict[int, List[str]]] = {
    # Lite procedures: identical Day 1 and Day 2, all four STK files each day.
    "lite": {
        1: [
            "TR-BER", "TR-45V", "TR-HOP", "TR-90S", "TR-SLH", "TR-LUN",
            "TR-STA", "TR-HBW",
            "TE-45V", "TE-HBW", "TE-OLS", "TE-TLS", "TE-HOP",
            "TR-STK1", "TR-STK2", "TR-STK3", "TR-STK4", "TR-TIL", "TR-PLY",
        ],
        2: [
            "TR-BER", "TR-45V", "TR-HOP", "TR-90S", "TR-SLH", "TR-LUN",
            "TR-STA", "TR-HBW",
            "TE-45V", "TE-HBW", "TE-OLS", "TE-TLS", "TE-HOP",
            "TR-STK1", "TR-STK2", "TR-STK3", "TR-STK4", "TR-TIL", "TR-PLY",
        ],
    },
    # LP procedures: Day 1 uses STK1+STK2; Day 2 uses STK3+STK4 in reversed order.
    "lp": {
        1: [
            "TR-BER", "TR-45V", "TR-90S", "TR-STA",
            "TE-45V", "TE-HBW", "TE-OLS", "TE-TLS", "TE-HOP",
            "TR-HOP", "TR-SLH", "TR-LUN", "TR-LBJ", "TR-HBW",
            "TR-STK1", "TR-STK2", "TR-TIL", "TR-PLY",
        ],
        2: [
            "TR-PLY", "TR-TIL", "TR-STK3", "TR-STK4",
            "TR-HBW", "TR-LBJ", "TR-LUN", "TR-SLH", "TR-HOP",
            "TE-HOP", "TE-TLS", "TE-OLS", "TE-HBW", "TE-45V",
            "TR-STA", "TR-90S", "TR-45V", "TR-BER",
        ],
    },
    # XL procedures: identical to LP per the current calibration spec.
    "xl": {
        1: [
            "TR-BER", "TR-45V", "TR-90S", "TR-STA",
            "TE-45V", "TE-HBW", "TE-OLS", "TE-TLS", "TE-HOP",
            "TR-HOP", "TR-SLH", "TR-LUN", "TR-LBJ", "TR-HBW",
            "TR-STK1", "TR-STK2", "TR-TIL", "TR-PLY",
        ],
        2: [
            "TR-PLY", "TR-TIL", "TR-STK3", "TR-STK4",
            "TR-HBW", "TR-LBJ", "TR-LUN", "TR-SLH", "TR-HOP",
            "TE-HOP", "TE-TLS", "TE-OLS", "TE-HBW", "TE-45V",
            "TR-STA", "TR-90S", "TR-45V", "TR-BER",
        ],
    },
}


def default_expected_for(
    family: Optional[str], session_number: int,
) -> List[str]:
    """Resolve the built-in default for one (family, session_number) pair.
    Falls back to the second-day procedure of the same family if a higher
    session number isn't explicitly defined, and to DEFAULT_EXPECTED if
    the family isn't known. Never raises."""
    if family and family in DEFAULT_BY_TYPE_AND_SESSION:
        by_session = DEFAULT_BY_TYPE_AND_SESSION[family]
        if session_number in by_session:
            return list(by_session[session_number])
        # Higher session # than we have data for → reuse last known.
        if by_session:
            last = max(by_session.keys())
            return list(by_session[last])
    return list(DEFAULT_EXPECTED)


# Filename pattern: <device>-<TR|TE>-<ACTIVITY>_<date>.csv[.gz]
# Examples: "10-00000002-TR-45V_03.23.2026.csv.gz", "10-00000002-TE-HBW_03.23.2026.csv"
_ACTIVITY_FROM_FILENAME = re.compile(
    r"-(?P<kind>TR|TE)-(?P<code>[A-Za-z0-9]+)_"
)


def parse_activity_from_key(key: str) -> str | None:
    """Extract the activity_id (e.g. 'TR-BER') from a bucket key or filename.
    Returns None if no recognizable activity marker is found."""
    name = key.rsplit("/", 1)[-1]
    m = _ACTIVITY_FROM_FILENAME.search(name)
    if not m:
        return None
    return f"{m.group('kind')}-{m.group('code')}"


__all__ = [
    "ACTIVITIES",
    "DEFAULT_EXPECTED",
    "DEFAULT_BY_TYPE_AND_SESSION",
    "TYPE_ID_TO_FAMILY",
    "activity_description",
    "parse_activity_from_key",
    "family_for_type_id",
    "family_for_device_id",
    "default_expected_for",
]
