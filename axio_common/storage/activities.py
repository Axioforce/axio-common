"""
Canonical calibration-activity registry.

Lives in axio-common so the DAQ, axio-server, and any analysis tool all
agree on what activities exist, what each one means, and the expected
set per (plate_family, session_number) for a complete calibration day.

Plate families are the calibration-procedure groupings:
    "lite"   — type ids 06, 10
    "lp"     — type ids 07, 11 (Launchpad)
    "xl"     — type ids 08, 12
    "insole" — type ids 09 (Left), 0a (Right)

The expected set for a session is resolved as:
    1. If the session's tests.txt enumerated TR-/TE- activity codes, use
       those (parsed at sync time, stored on CalibrationBucketSession).
    2. If <bucket>/_config/session_expected_activities.json has an entry for
       (plate_family, session_number), use it.
    3. Otherwise fall back to DEFAULT_BY_TYPE_AND_SESSION[plate_family][N].
    4. As a last resort fall back to DEFAULT_EXPECTED (all activities).

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
        # Insole-family training activities (type ids 09, 0a). Order/spec from
        # the Insole Calibration Procedure tests.txt; auto-mirrored to test
        # files in reverse order ("Testing done in reverse order to train").
        "MDS":  "Multi-directional steps",
        "MIP":  "March in place",
        "ADB":  "Adduction / abduction",
        "CHR":  "Chair (sit down / stand up)",
        "STL":  "Stanky leg + foot rotations",
        "LAT":  "Lateral lunges",
        "LNG":  "Forward lunges",
        "WLK":  "Walking",
        "SQT":  "Squats",
        "QST":  "Quiet stand",
        "WDL":  "Waddle steps",
        "WGT":  "Weighted waddle steps + weighted quiet stand + weighted walking",
        "TOE":  "Toe taps + toe balance",
    },
    "test": {
        "45V": "45 lb dumbbell vertical, 5×4 grid, snake from front-left, 30s total (lift the dumbbell, don't slide it)",
        "HBW": "Half body weight, 4×4 grid, 45s each side, two partial-bodyweight levels per position",
        "OLS": "One-leg stand — 4 corners + center, facing 8 directions, 30s",
        "TLS": "Two-leg stand — both heels in corners + center, facing 8 directions, 30s",
        "HOP": "Hop covering full plate in 4 directions, 30s",
        # Insole test activities mirror the train side (procedure repeats each
        # capture in reverse order); same descriptions, just different files.
        "MDS": "Multi-directional steps",
        "MIP": "March in place",
        "ADB": "Adduction / abduction",
        "CHR": "Chair (sit down / stand up)",
        "STL": "Stanky leg + foot rotations",
        "LAT": "Lateral lunges",
        "LNG": "Forward lunges",
        "WLK": "Walking",
        "SQT": "Squats",
        "QST": "Quiet stand",
        "WDL": "Waddle steps",
        "WGT": "Weighted waddle + weighted quiet stand + weighted walking",
        "TOE": "Toe taps + toe balance",
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
    "09": "insole",   # Shoe Insole Left
    "0a": "insole",   # Shoe Insole Right
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
    # Insole procedure: per the Insole Calibration Procedure tests.txt, one
    # capture session contains all train activities followed by their test
    # mirrors in reverse order. This is the fallback when a session's own
    # tests.txt didn't parse — newer sessions should drive their list from
    # their own tests.txt via parse_expected_activities_from_tests_txt().
    "insole": {
        1: [
            "TR-MDS", "TR-MIP", "TR-ADB", "TR-CHR", "TR-STL", "TR-LAT",
            "TR-LNG", "TR-WLK", "TR-SQT", "TR-QST", "TR-WDL", "TR-WGT",
            "TR-TOE",
            "TE-TOE", "TE-WGT", "TE-WDL", "TE-QST", "TE-SQT", "TE-WLK",
            "TE-LNG", "TE-LAT", "TE-STL", "TE-CHR", "TE-ADB", "TE-MIP",
            "TE-MDS",
        ],
    },
}


# Activity-line shape inside a tests.txt body — `TR-XXX:` or `TE-XXX:` at
# (optionally indented) line start. Case-insensitive in case operators
# typed lowercase. The code may end with `-N` (a numeric range upper-bound
# like `STK1-4`) — captured here and expanded by _expand_range_code below.
_ACTIVITY_LINE_RE = re.compile(
    r"^\s*(TR|TE)-([A-Za-z0-9]+(?:-\d+)?)\s*:",
    re.MULTILINE | re.IGNORECASE,
)
# "Testing done in reverse order to train" / "test files in reverse order" —
# loose match because operators don't always phrase it identically.
_REVERSE_ORDER_HINT_RE = re.compile(
    r"\breverse\s+order\b", re.IGNORECASE,
)
# Detect range shorthand inside a captured code: `STK1-4` → prefix=STK, lo=1,
# hi=4. The prefix can be empty (e.g. just `1-4`); the parser still expands
# but a sane callable should treat that as nonsense.
_RANGE_CODE_RE = re.compile(r"^([A-Za-z]+)(\d+)-(\d+)$")


def _expand_range_code(code: str) -> List[str]:
    """`'STK1-4'` → `['STK1','STK2','STK3','STK4']`. Codes without a range
    return as a single-element list. lo > hi or runaway ranges (>50 items)
    fall through unchanged — those are almost certainly bad input rather
    than a legitimate range."""
    m = _RANGE_CODE_RE.match(code)
    if not m:
        return [code]
    prefix, lo, hi = m.group(1), int(m.group(2)), int(m.group(3))
    if lo > hi or hi - lo > 50:
        return [code]
    return [f"{prefix}{n}" for n in range(lo, hi + 1)]


def parse_expected_activities_from_tests_txt(body: str) -> List[str]:
    """Derive the expected activity list for a session by parsing its
    tests.txt. Returns a list of joined activity ids ("TR-MDS", "TE-MDS",
    "TR-STK1", "TR-STK2", …) in the order they appear in the file.

    Recognized shapes:
      (a) Train and test activities both enumerated explicitly
          (`TR-BER:` / `TE-HOP:`). Returned in document order.
      (b) Only train activities enumerated, with a hint like
          "Testing done in reverse order to train". The parser mirrors the
          train list into TE-* in reversed order.
      (c) Range shorthand in the activity code: `TR-STK1-4: …` expands to
          TR-STK1, TR-STK2, TR-STK3, TR-STK4 (in that order) where the file
          line originally appeared.

    Returns [] when the body is empty or no TR-/TE- lines are present —
    callers should fall back to the family default in that case."""
    if not body:
        return []
    train_codes: List[str] = []
    test_codes: List[str] = []
    seen: set = set()
    for m in _ACTIVITY_LINE_RE.finditer(body):
        kind = m.group(1).upper()
        raw_code = m.group(2).upper()
        for sub in _expand_range_code(raw_code):
            joined = f"{kind}-{sub}"
            if joined in seen:
                continue
            seen.add(joined)
            if kind == "TR":
                train_codes.append(sub)
            else:
                test_codes.append(sub)
    trains = [f"TR-{c}" for c in train_codes]
    tests = [f"TE-{c}" for c in test_codes]
    if not tests and train_codes and _REVERSE_ORDER_HINT_RE.search(body):
        tests = [f"TE-{c}" for c in reversed(train_codes)]
    return trains + tests


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
    "parse_expected_activities_from_tests_txt",
    "family_for_type_id",
    "family_for_device_id",
    "default_expected_for",
]
