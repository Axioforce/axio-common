"""The six-axis load cell's corner of the calibration bucket (#1408).

Contract 6, `axioforce-loadcell-firmware/docs/SERVER_RECORDS.md`, and the
writer, AxioTDK `src/loadcell_sensor/records.py` (#1399). A load cell is
captured **before its type is known**, so its records are keyed by the serial
alone under a top-level `loadcell/` family instead of `<type>/<device>/`, and
nothing is renamed when the unit is typed:

    loadcell/<serial>/<iso-date>/train/<serial>-TR-<ACT>_<MM.DD.YYYY>.csv.gz
    loadcell/<serial>/<iso-date>/test/<serial>-TE-<ACT>_<MM.DD.YYYY>.csv.gz
    loadcell/<serial>/<iso-date>/tests.txt
    loadcell/<serial>/<iso-date>/axiosaurus/<capture>_{loadcell.json,program.gcode,gantry_sim.json}
    loadcell/<serial>/models/...                         (training output)

A **test upload** (anything simulated: AxioTDK `--test-upload`) puts
`test-<iso-date>` in the date segment. Such a session is listed, flagged
`is_test`, and is **never calibration data**: `loadcell_capture_keys_for_job`
refuses it and nothing here offers it as a session to train on.

The cache mirrors the bucket the usual way (`cache_path_for_key`: dotted date
directory, a `calibration_data/` layer, `.csv.gz` -> `.csv`); a test segment
is not an ISO date and stays as it is. Map back with
`bucket_key_for_cache_path`, never by using a cache path as a key.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from . import storage_core as _sc
from .activities import (
    LOADCELL_PREFIX, parse_activity_from_key,
    parse_expected_activities_from_tests_txt,
)

#: The date segment of a test upload: `test-<iso-date>`.
LOADCELL_TEST_SEGMENT_PREFIX = "test-"
#: The Axiosaurus sidecar folder inside a session (same name as the grid's).
LOADCELL_SIDECAR_DIR = "axiosaurus"
LOADCELL_MODELS_DIR = "models"

#: What a key under `loadcell/` is.
PART_TRAIN = "train"
PART_TEST = "test"
PART_TESTS_TXT = "tests_txt"
PART_SIDECAR = "sidecar"
PART_OTHER = "other"          # inside a session, but none of the above
PART_MODEL = "model"          # loadcell/<serial>/models/...
LOADCELL_PARTS = (PART_TRAIN, PART_TEST, PART_TESTS_TXT, PART_SIDECAR,
                  PART_OTHER, PART_MODEL)

_SERIAL_RE = re.compile(r"^[0-9a-f]{8}$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CAPTURE_NAME_RE = re.compile(r"\.csv(?:\.gz)?$")
_DOTTED_RE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$")


def _check_serial(serial) -> str:
    s = str(serial).strip().lower()
    if not _SERIAL_RE.match(s):
        raise ValueError(
            f"load cell serial must be 8 lowercase hex digits, got {serial!r}")
    return s


def parse_date_segment(segment: str):
    """`(iso_date, is_test)` for a session's date segment, or None.

    `2026-11-10` -> ("2026-11-10", False); `test-2026-11-09` ->
    ("2026-11-09", True); anything else (a dotted cache date, `models`, a
    typo) -> None, so it is never mistaken for a session.
    """
    if _ISO_RE.match(segment or ""):
        return segment, False
    if (segment or "").startswith(LOADCELL_TEST_SEGMENT_PREFIX):
        iso = segment[len(LOADCELL_TEST_SEGMENT_PREFIX):]
        if _ISO_RE.match(iso):
            return iso, True
    return None


def date_segment(iso_date: str, test: bool = False) -> str:
    """The bucket's date segment for a session (AxioTDK's spelling)."""
    if not _ISO_RE.match(iso_date or ""):
        raise ValueError(f"iso date must be YYYY-MM-DD, got {iso_date!r}")
    return f"{LOADCELL_TEST_SEGMENT_PREFIX}{iso_date}" if test else iso_date


def loadcell_serial_prefix(serial) -> str:
    """`loadcell/<serial>/`."""
    return f"{LOADCELL_PREFIX}{_check_serial(serial)}/"


def loadcell_session_prefix(serial, segment: str) -> str:
    """`loadcell/<serial>/<segment>/`; ValueError for a segment that is not a
    session (an ISO date or `test-<iso-date>`)."""
    if parse_date_segment(segment) is None:
        raise ValueError(f"not a load cell session date segment: {segment!r}")
    return f"{loadcell_serial_prefix(serial)}{segment}/"


def loadcell_models_prefix(serial) -> str:
    """`loadcell/<serial>/models/`: where a load cell job's output lives."""
    return f"{loadcell_serial_prefix(serial)}{LOADCELL_MODELS_DIR}/"


@dataclass(frozen=True)
class LoadCellKey:
    """A bucket key under `loadcell/`, taken apart."""
    key: str
    serial: str
    part: str                      # one of LOADCELL_PARTS
    segment: Optional[str] = None  # the date segment; None for a model key
    iso_date: Optional[str] = None
    is_test: bool = False
    name: str = ""                 # the file name
    #: Joined activity ("TR-GRID") for a train/test capture, else None.
    activity: Optional[str] = None

    @property
    def is_capture(self) -> bool:
        return self.part in (PART_TRAIN, PART_TEST) and \
            bool(_CAPTURE_NAME_RE.search(self.name))

    @property
    def session_prefix(self) -> Optional[str]:
        if self.segment is None:
            return None
        return f"{LOADCELL_PREFIX}{self.serial}/{self.segment}/"


def parse_loadcell_key(key: str) -> Optional[LoadCellKey]:
    """Take a `loadcell/...` key apart; None for anything this layout does not
    describe (another family, a folder marker, a malformed serial, a date
    segment that is neither ISO nor `test-<iso>`). Never raises."""
    if not isinstance(key, str) or not key.startswith(LOADCELL_PREFIX):
        return None
    if _sc.is_folder_marker(key):
        return None
    parts = key.split("/")
    if len(parts) < 4 or not _SERIAL_RE.match(parts[1]):
        return None
    serial, third = parts[1], parts[2]
    name = parts[-1]
    if not name:
        return None
    if third == LOADCELL_MODELS_DIR:
        return LoadCellKey(key=key, serial=serial, part=PART_MODEL, name=name)
    parsed = parse_date_segment(third)
    if parsed is None:
        return None
    iso, is_test = parsed
    rest = parts[3:]
    activity = None
    if len(rest) == 1 and rest[0] == "tests.txt":
        part = PART_TESTS_TXT
    elif len(rest) >= 2 and rest[0] in (PART_TRAIN, PART_TEST):
        part = rest[0]
        if _CAPTURE_NAME_RE.search(name):
            activity = parse_activity_from_key(key)
    elif len(rest) >= 2 and rest[0] == LOADCELL_SIDECAR_DIR:
        part = PART_SIDECAR
    else:
        part = PART_OTHER
    return LoadCellKey(key=key, serial=serial, part=part, segment=third,
                       iso_date=iso, is_test=is_test, name=name,
                       activity=activity)


def loadcell_cache_relative(key: str) -> str:
    """The cache-relative path (forward slashes) a job config names for a
    bucket key: `loadcell/<serial>/<MM.DD.YYYY>/calibration_data/train/
    <capture>.csv`. The daemon re-roots it onto its own cache."""
    return _sc.cache_path_for_key(key, Path("")).as_posix()


def loadcell_session_for_cache_path(path) -> Optional[tuple]:
    """`(serial, segment)` of the session a cache path (absolute, relative, or
    under another machine's cache root) lies in, or None. The inverse the
    daemon needs to download a load cell job's sessions."""
    parts = Path(str(path)).as_posix().split("/")
    for i in range(len(parts) - 2):
        if parts[i] != LOADCELL_PREFIX.rstrip("/"):
            continue
        serial, seg = parts[i + 1].lower(), parts[i + 2]
        if not _SERIAL_RE.match(serial):
            continue
        if _DOTTED_RE.match(seg):
            seg = _sc._iso_from_dotted(seg)
        if parse_date_segment(seg) is not None:
            return serial, seg
    return None


# --- tests.txt ---------------------------------------------------------------

#: tests.txt header lines AxioTDK writes (records.tests_txt), by the field
#: name `parse_loadcell_tests_txt` returns them under.
TESTS_TXT_HEADER = {
    "serial": "Serial",
    "mcu_uid": "MCU UID",
    "board_rev": "Board rev",
    "firmware": "Firmware",
    "reference": "Reference",
    "device_type": "Device type",
    "session_file": "Session file",
    "calibrated_by": "Calibrated by",
}
_HEADER_BY_LABEL = {v.lower(): k for k, v in TESTS_TXT_HEADER.items()}
_KV_RE = re.compile(r"^\s*([^:]+?)\s*:\s*(.*?)\s*$")
_ACTIVITY_LINE_RE = re.compile(r"^\s*(TR|TE)-", re.IGNORECASE)


def parse_loadcell_tests_txt(body: str) -> Dict[str, object]:
    """A load cell session's tests.txt, read the way the bucket sync stores it.

    Returns `{"header": {field: value}, "activities": ["TR-GRID", ...],
    "descriptions": {"TR-GRID": "normal grid, ..."}}`. Header fields are the
    keys of `TESTS_TXT_HEADER` for the lines AxioTDK writes (an unknown header
    line is kept under its lowercased, underscored label); `device_type` keeps
    its text ("00 (uncalibrated)"). The activity list is axio-common's own
    parser's (`parse_expected_activities_from_tests_txt`), so the dashboard
    and the job agree on it. Never raises on odd input.
    """
    header: Dict[str, str] = {}
    descriptions: Dict[str, str] = {}
    for line in (body or "").splitlines():
        if not line.strip():
            continue
        m = _KV_RE.match(line)
        if not m:
            continue
        label, value = m.group(1), m.group(2)
        if _ACTIVITY_LINE_RE.match(line):
            descriptions.setdefault(label.strip().upper(), value)
            continue
        key = _HEADER_BY_LABEL.get(label.strip().lower()) or \
            label.strip().lower().replace(" ", "_").replace("-", "_")
        header.setdefault(key, value)
    if "serial" in header:
        header["serial"] = header["serial"].strip().lower()
    if "mcu_uid" in header:
        header["mcu_uid"] = header["mcu_uid"].strip().lower()
    return {"header": header,
            "activities": parse_expected_activities_from_tests_txt(body or ""),
            "descriptions": descriptions}


# --- listing and download (either backend) ------------------------------------

@dataclass
class LoadCellSessionListing:
    serial: str
    segment: str
    iso_date: str
    is_test: bool
    train: List[str] = field(default_factory=list)
    test: List[str] = field(default_factory=list)
    tests_txt: Optional[str] = None
    sidecars: List[str] = field(default_factory=list)
    other: List[str] = field(default_factory=list)

    def all_keys(self) -> List[str]:
        keys = list(self.train) + list(self.test) + list(self.sidecars) \
            + list(self.other)
        if self.tests_txt:
            keys.append(self.tests_txt)
        return keys


def list_loadcell_serials() -> List[str]:
    """Serials that have anything under `loadcell/`, sorted."""
    keys = _sc._list_keys_recursive(LOADCELL_PREFIX)
    return sorted({p.serial for p in map(parse_loadcell_key, keys) if p})


def list_loadcell_sessions(serial, include_test: bool = False) -> List[str]:
    """A unit's session date segments, sorted. Test segments only when asked:
    they are never calibration data."""
    keys = _sc._list_keys_recursive(loadcell_serial_prefix(serial))
    segs = {p.segment for p in map(parse_loadcell_key, keys)
            if p and p.segment and (include_test or not p.is_test)}
    return sorted(segs)


def list_loadcell_session(serial, segment: str) -> LoadCellSessionListing:
    """Every key of one session, split by part. Folder markers dropped."""
    prefix = loadcell_session_prefix(serial, segment)
    iso, is_test = parse_date_segment(segment)
    out = LoadCellSessionListing(serial=_check_serial(serial),
                                 segment=segment, iso_date=iso,
                                 is_test=is_test)
    for k in _sc._list_keys_recursive(prefix):
        p = parse_loadcell_key(k)
        if p is None:
            continue
        if p.part == PART_TRAIN:
            out.train.append(k)
        elif p.part == PART_TEST:
            out.test.append(k)
        elif p.part == PART_TESTS_TXT:
            out.tests_txt = k
        elif p.part == PART_SIDECAR:
            out.sidecars.append(k)
        else:
            out.other.append(k)
    for lst in (out.train, out.test, out.sidecars, out.other):
        lst.sort()
    return out


def download_loadcell_session(
    serial, segment: str, cache_root: Path | None = None, *,
    progress: Callable | None = None, on_bytes=None, workers: int = 8,
) -> Path:
    """Make a whole session local (captures, tests.txt, sidecars) and return
    its cache directory (`<cache>/loadcell/<serial>/<MM.DD.YYYY>`, or the
    test segment as it is). Raises what `download_files` raises."""
    listing = list_loadcell_session(serial, segment)
    _sc.download_files(listing.all_keys(), cache_root=cache_root,
                       progress=progress, on_bytes=on_bytes, workers=workers)
    root = Path(cache_root) if cache_root else _sc.DEFAULT_CACHE_ROOT
    return root / Path(_sc._translate_iso_dates(
        loadcell_session_prefix(serial, segment).rstrip("/")))


# --- what a training job may name --------------------------------------------

class LoadCellJobInputError(ValueError):
    """A capture a load cell job may not train on (test data, another unit,
    not a capture). Nothing is submitted."""


def loadcell_capture_keys_for_job(serial, keys: Sequence[str]) -> Dict[str, List[str]]:
    """Split the bucket keys a job is to train on into `{"train": [...],
    "test": [...]}` (cache-relative paths are made from these), refusing
    anything that is not one of THIS unit's real calibration captures:
    a key under a `test-<iso>` segment, another serial's key, a sidecar,
    tests.txt or model file. Order is kept; duplicates are dropped."""
    serial = _check_serial(serial)
    out: Dict[str, List[str]] = {PART_TRAIN: [], PART_TEST: []}
    seen = set()
    for k in keys:
        if k in seen:
            continue
        seen.add(k)
        p = parse_loadcell_key(k)
        if p is None or p.serial != serial:
            raise LoadCellJobInputError(
                f"{k!r} is not a capture of load cell {serial}")
        if p.is_test:
            raise LoadCellJobInputError(
                f"{k!r} is test data (a test-<date> upload), never "
                f"calibration data")
        if not p.is_capture:
            raise LoadCellJobInputError(f"{k!r} is not a train/test capture")
        out[p.part].append(k)
    return out


__all__ = [
    "LOADCELL_TEST_SEGMENT_PREFIX", "LOADCELL_SIDECAR_DIR",
    "LOADCELL_MODELS_DIR", "LOADCELL_PARTS",
    "PART_TRAIN", "PART_TEST", "PART_TESTS_TXT", "PART_SIDECAR", "PART_OTHER",
    "PART_MODEL", "TESTS_TXT_HEADER",
    "LoadCellKey", "LoadCellSessionListing", "LoadCellJobInputError",
    "parse_date_segment", "date_segment", "loadcell_serial_prefix",
    "loadcell_session_prefix", "loadcell_models_prefix", "parse_loadcell_key",
    "loadcell_cache_relative", "loadcell_session_for_cache_path",
    "parse_loadcell_tests_txt", "list_loadcell_serials",
    "list_loadcell_sessions", "list_loadcell_session",
    "download_loadcell_session", "loadcell_capture_keys_for_job",
]
