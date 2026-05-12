"""
Canonical calibration-activity registry.

Lives in axio-common so the DAQ, axio-server, and any analysis tool all
agree on what activities exist, what each one means, and which set is
expected in a complete session.

Per-session overrides live in the bucket as
    <type>/<device>/<date>/expected_activities.json
with shape:
    {"expected": ["TR-BER", "TR-45V", ...]}

If that file doesn't exist for a session, DEFAULT_EXPECTED is used.
"""
from __future__ import annotations

import re
from typing import Dict, List

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


# Default expected set when a session has no expected_activities.json
# override. All 19 activities in order: trains first, then tests.
DEFAULT_EXPECTED: List[str] = (
    [f"TR-{code}" for code in ACTIVITIES["train"].keys()]
    + [f"TE-{code}" for code in ACTIVITIES["test"].keys()]
)


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
    "activity_description",
    "parse_activity_from_key",
]
