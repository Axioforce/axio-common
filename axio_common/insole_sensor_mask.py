"""Insole sensor mask encoding.

Insoles can develop arbitrary pressure-sensor failures over time, so every
data file and trained model carries a hex-encoded suffix recording (a)
which of the 15 magnetometer pressure sensors are present and (b) whether
the IMU's accelerometer and gyroscope channels are included.

Single source of truth for the encoding lives here. Both the calibration
file-naming tooling (offline auto-detection of dead sensors) and the
training/inference code (NeuralNet.py, RunNNLiteGUI.py) import from this
module — never reimplement the math elsewhere.

See INSOLE_SENSOR_MASK.md at the axio-common repo root for the prose
specification and usage guide.

Run the doctests with `python -m doctest insole_sensor_mask.py -v`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Tuple, Union

#: Number of pressure sensors on an insole. IDs are 1..15. Sensor 16 is
#: the "control mag" reference and is intentionally never part of the
#: trained feature set, so it has no bit in the mask.
PRESSURE_BITS: int = 15

#: Bitmask value with every pressure sensor present (1..15).
ALL_SENSORS_MASK: int = (1 << PRESSURE_BITS) - 1  # 0x7fff

# Regex matching the canonical filename suffix. Anchored to end-of-stem so
# we never match an `m<hex>` that happens to appear mid-name. Lowercase
# hex only — filesystems differ on case-sensitivity and we want files to
# round-trip identically through Windows, Linux and Tigris.
_SUFFIX_RE = re.compile(r"_m(?P<hex>[0-9a-f]{4})(?:_(?P<imu>a|g|ag))?$")


# Forward declaration so the constant below can reference the dataclass.
@dataclass(frozen=True)
class InsoleFeatures:
    """The resolved feature spec a model was trained on or a CSV was
    captured with. Symmetric across encode/decode."""

    #: 1-indexed sensor IDs, always sorted ascending.
    sensors: Tuple[int, ...]
    include_accel: bool
    include_gyro: bool

    @property
    def mask(self) -> int:
        """Pressure-sensor bitmask (0..ALL_SENSORS_MASK)."""
        return sensors_to_mask(self.sensors)

    @property
    def feature_count(self) -> int:
        """Total NN input columns the spec implies (3 axes per sensor +
        3 per IMU group)."""
        return (
            len(self.sensors) * 3
            + (3 if self.include_accel else 0)
            + (3 if self.include_gyro else 0)
        )


#: The "everything on" feature spec: every pressure sensor present, accel
#: and gyro both included. Used as the fallback whenever a filename has no
#: parseable suffix — see `features_from_filename` and the README.
FULL_INSOLE_FEATURES: "InsoleFeatures" = InsoleFeatures(
    sensors=tuple(range(1, PRESSURE_BITS + 1)),
    include_accel=True,
    include_gyro=True,
)


def sensors_to_mask(sensors: Iterable[int]) -> int:
    """Pack a collection of 1-indexed sensor IDs into a bitmask.

    Bit (i-1) represents sensor i. Sensors outside 1..PRESSURE_BITS raise
    ValueError. Duplicates are silently collapsed.

    >>> hex(sensors_to_mask([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]))
    '0x7fff'
    >>> hex(sensors_to_mask([1, 3, 5]))
    '0x15'
    >>> hex(sensors_to_mask([5, 1, 3, 1]))   # order and dups don't matter
    '0x15'
    >>> sensors_to_mask([])
    0
    >>> sensors_to_mask([16])
    Traceback (most recent call last):
        ...
    ValueError: sensor 16 out of range 1..15
    """
    m = 0
    for s in sensors:
        if not 1 <= s <= PRESSURE_BITS:
            raise ValueError(f"sensor {s} out of range 1..{PRESSURE_BITS}")
        m |= 1 << (s - 1)
    return m


def mask_to_sensors(mask: int) -> Tuple[int, ...]:
    """Unpack a bitmask into a sorted tuple of 1-indexed sensor IDs.

    >>> mask_to_sensors(0x7fff)
    (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)
    >>> mask_to_sensors(0x15)
    (1, 3, 5)
    >>> mask_to_sensors(0)
    ()
    >>> mask_to_sensors(0x8000)
    Traceback (most recent call last):
        ...
    ValueError: mask 0x8000 outside valid range 0..0x7fff
    """
    if not 0 <= mask <= ALL_SENSORS_MASK:
        raise ValueError(
            f"mask {mask:#x} outside valid range 0..{ALL_SENSORS_MASK:#x}"
        )
    return tuple(i + 1 for i in range(PRESSURE_BITS) if mask & (1 << i))


def encode_filename_suffix(
    sensors: Iterable[int], *, include_accel: bool, include_gyro: bool,
) -> str:
    """Return the canonical filename suffix for the given feature set.

    Always begins with an underscore so it concatenates cleanly onto an
    existing stem. The hex mask is zero-padded to 4 lowercase chars; the
    IMU tail is `_ag` / `_a` / `_g` and is omitted entirely when neither
    accel nor gyro is present.

    >>> encode_filename_suffix(range(1, 16), include_accel=True, include_gyro=True)
    '_m7fff_ag'
    >>> encode_filename_suffix(range(1, 16), include_accel=False, include_gyro=False)
    '_m7fff'
    >>> encode_filename_suffix([1, 2, 3], include_accel=True, include_gyro=False)
    '_m0007_a'
    >>> encode_filename_suffix([], include_accel=False, include_gyro=True)
    '_m0000_g'
    """
    mask = sensors_to_mask(sensors)
    imu = ("a" if include_accel else "") + ("g" if include_gyro else "")
    return f"_m{mask:04x}" + (f"_{imu}" if imu else "")


def parse_filename_suffix(filename: Union[str, Path]) -> Optional[InsoleFeatures]:
    """Parse the canonical suffix off a filename or path.

    Accepts a full path, basename, or bare stem. Returns the resolved
    feature spec, or None if no recognizable suffix is present. Most
    callers want `features_from_filename` instead, which applies the
    project-wide "no suffix means everything on" policy. Use this strict
    version only when you specifically need to distinguish "legacy
    pre-convention file" from "explicitly stamped file".

    >>> parse_filename_suffix('TR-BER_2026-05-21_m7fff_ag.csv')
    InsoleFeatures(sensors=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15), include_accel=True, include_gyro=True)
    >>> parse_filename_suffix('best-model_m0007.h5')
    InsoleFeatures(sensors=(1, 2, 3), include_accel=False, include_gyro=False)
    >>> parse_filename_suffix('legacy-file.csv') is None
    True
    >>> parse_filename_suffix('/some/where/sess_m000f_g.parquet').include_accel
    False
    """
    if isinstance(filename, (str, Path)):
        # Discard parent directories and extension.
        stem = Path(filename).stem
    else:
        raise TypeError(f"filename must be str or Path, got {type(filename).__name__}")
    m = _SUFFIX_RE.search(stem)
    if not m:
        return None
    mask = int(m.group("hex"), 16)
    imu = m.group("imu") or ""
    return InsoleFeatures(
        sensors=mask_to_sensors(mask),
        include_accel="a" in imu,
        include_gyro="g" in imu,
    )


def features_from_filename(filename: Union[str, Path]) -> InsoleFeatures:
    """Resolve the feature spec for a file, applying the project-wide
    legacy fallback: if no `_m<hex>` suffix is present, return
    `FULL_INSOLE_FEATURES` (every pressure sensor + accel + gyro).

    Use this as the default entry point for any consumer that just wants
    to know "which features does this file have?" without caring about
    the legacy/explicit distinction. The two callers that matter today
    (live inference reading a CSV, model loader picking columns) both
    want this behavior.

    >>> features_from_filename('TR-BER_2026-05-21_m7ffd_ag.csv').mask
    32765
    >>> features_from_filename('TR-BER_2026-05-21_m0007_a.csv').sensors
    (1, 2, 3)
    >>> features_from_filename('legacy-with-no-suffix.csv') == FULL_INSOLE_FEATURES
    True
    >>> features_from_filename('legacy-with-no-suffix.csv').include_gyro
    True
    """
    parsed = parse_filename_suffix(filename)
    return parsed if parsed is not None else FULL_INSOLE_FEATURES


def stamp_filename(
    filename: Union[str, Path],
    sensors: Iterable[int],
    *,
    include_accel: bool,
    include_gyro: bool,
) -> str:
    """Return `filename` with the canonical suffix applied before the
    extension. If the input already carries a parseable suffix it is
    REPLACED rather than concatenated, so the output is always canonical.

    Parent directories are preserved.

    >>> stamp_filename('TR-BER_2026-05-21.csv',
    ...                [1, 2, 3], include_accel=True, include_gyro=False)
    'TR-BER_2026-05-21_m0007_a.csv'
    >>> stamp_filename('TR-BER_2026-05-21_m7fff_ag.csv',
    ...                [1, 2, 3], include_accel=True, include_gyro=False)
    'TR-BER_2026-05-21_m0007_a.csv'
    """
    p = Path(filename)
    stem = _SUFFIX_RE.sub("", p.stem)
    suffix = encode_filename_suffix(
        sensors, include_accel=include_accel, include_gyro=include_gyro,
    )
    return str(p.with_name(f"{stem}{suffix}{p.suffix}"))


def feature_columns_for(features: InsoleFeatures) -> list[str]:
    """Materialize the ordered list of NN input column names for a given
    feature spec. Stable across train/inference — every caller that
    builds a feature matrix from an insole file should use this so the
    column order is always the same.

    >>> feature_columns_for(InsoleFeatures((1, 2), True, False))
    ['sensor-1-x', 'sensor-2-x', 'sensor-1-y', 'sensor-2-y', 'sensor-1-z', 'sensor-2-z', 'Ax', 'Ay', 'Az']
    """
    sensors = features.sensors
    cols: list[str] = []
    cols += [f"sensor-{i}-x" for i in sensors]
    cols += [f"sensor-{i}-y" for i in sensors]
    cols += [f"sensor-{i}-z" for i in sensors]
    if features.include_accel:
        cols += ["Ax", "Ay", "Az"]
    if features.include_gyro:
        cols += ["Gx", "Gy", "Gz"]
    return cols


def features_from_config(config: dict) -> InsoleFeatures:
    """Resolve INSOLE_SENSORS / INSOLE_INCLUDE_ACCEL / INSOLE_INCLUDE_GYRO
    out of a job config dict, applying the default-full-set fallback when
    keys are absent. Mirrors NeuralNet.py's logic so the daemon and the
    file-naming code can't disagree.

    >>> features_from_config({})
    InsoleFeatures(sensors=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15), include_accel=True, include_gyro=True)
    >>> features_from_config({'INSOLE_SENSORS': [1, 2, 3],
    ...                       'INSOLE_INCLUDE_ACCEL': False,
    ...                       'INSOLE_INCLUDE_GYRO': True})
    InsoleFeatures(sensors=(1, 2, 3), include_accel=False, include_gyro=True)
    """
    raw = config.get("INSOLE_SENSORS")
    if raw is None:
        sensors: Tuple[int, ...] = tuple(range(1, PRESSURE_BITS + 1))
    else:
        # Tolerate JSON-deserialized ints-as-strings; drop anything out of range.
        sensors = tuple(sorted({
            int(n) for n in raw if 1 <= int(n) <= PRESSURE_BITS
        }))
    return InsoleFeatures(
        sensors=sensors,
        include_accel=bool(config.get("INSOLE_INCLUDE_ACCEL", True)),
        include_gyro=bool(config.get("INSOLE_INCLUDE_GYRO", True)),
    )


__all__ = [
    "ALL_SENSORS_MASK",
    "FULL_INSOLE_FEATURES",
    "InsoleFeatures",
    "PRESSURE_BITS",
    "encode_filename_suffix",
    "feature_columns_for",
    "features_from_config",
    "features_from_filename",
    "mask_to_sensors",
    "parse_filename_suffix",
    "sensors_to_mask",
    "stamp_filename",
]


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------
#
# Run as `python -m axio_common.insole_sensor_mask <mask>` to decode any of:
#     7ffd          # bare hex mask
#     0x7ffd        # hex with 0x prefix
#     m7ffd         # mask with the `m` marker
#     m7ffd_ag      # mask + IMU tail
#     _m7ffd_a      # full filename suffix
# Omit the argument to drop into an interactive prompt.

_CLI_INPUT_RE = re.compile(
    r"^(?P<hex>[0-9a-f]{1,4})(?:_(?P<imu>a|g|ag))?$"
)


def _parse_cli_input(raw: str) -> Tuple[int, Optional[bool], Optional[bool]]:
    """Parse a free-form mask string a human might type or paste.

    Returns ``(mask, include_accel, include_gyro)`` where the IMU flags
    are ``None`` if the input doesn't specify them (e.g. a bare ``7ffd``
    leaves the IMU question open).

    >>> _parse_cli_input("7ffd")
    (32765, None, None)
    >>> _parse_cli_input("0x0007")
    (7, None, None)
    >>> _parse_cli_input("m7ffd_ag")
    (32765, True, True)
    >>> _parse_cli_input("_m000f_g")
    (15, False, True)
    """
    cleaned = raw.strip().lower()
    cleaned = cleaned.lstrip("_")
    if cleaned.startswith("m"):
        cleaned = cleaned[1:]
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]
    match = _CLI_INPUT_RE.match(cleaned)
    if not match:
        raise ValueError(
            f"not a valid mask: {raw!r}. "
            "Try a hex like 7ffd, m7ffd_ag, or 0x0007."
        )
    mask = int(match.group("hex"), 16)
    if mask > ALL_SENSORS_MASK:
        raise ValueError(
            f"mask 0x{mask:x} exceeds the 15-sensor maximum 0x{ALL_SENSORS_MASK:x}"
        )
    imu = match.group("imu")
    if imu is None:
        return mask, None, None
    return mask, "a" in imu, "g" in imu


def _print_decoded(
    mask: int, include_accel: Optional[bool], include_gyro: Optional[bool],
) -> None:
    """Pretty-print a parsed mask to stdout. Format chosen to be skimmable
    in a terminal — mask line, then a sensors-present row, then
    sensors-missing row, then IMU state."""
    sensors = mask_to_sensors(mask)
    missing = [i for i in range(1, PRESSURE_BITS + 1) if i not in sensors]
    binary = format(mask, f"0{PRESSURE_BITS}b")

    print(f"Mask:            0x{mask:04x}  (binary {binary})")
    print(
        f"Sensors present: "
        f"{', '.join(str(s) for s in sensors) if sensors else '(none)'}  "
        f"[{len(sensors)}/{PRESSURE_BITS}]"
    )
    if missing:
        print(
            f"Sensors missing: "
            f"{', '.join(str(m) for m in missing)}  "
            f"[{len(missing)}/{PRESSURE_BITS}]"
        )

    if include_accel is None and include_gyro is None:
        print("IMU:             unspecified (add an _ag / _a / _g tail to specify)")
    else:
        print(f"Accelerometer:   {'on' if include_accel else 'off'}")
        print(f"Gyroscope:       {'on' if include_gyro else 'off'}")
        feats = InsoleFeatures(
            sensors=tuple(sensors),
            include_accel=bool(include_accel),
            include_gyro=bool(include_gyro),
        )
        print(
            f"Feature count:   {feats.feature_count}  "
            f"({len(sensors)} sensors x 3"
            f"{' + 3 accel' if include_accel else ''}"
            f"{' + 3 gyro' if include_gyro else ''})"
        )
        print(
            f"Canonical suffix: {encode_filename_suffix(list(sensors), include_accel=bool(include_accel), include_gyro=bool(include_gyro))}"
        )


def _cli_main(argv: Optional[list] = None) -> int:
    """Entry point for `python -m axio_common.insole_sensor_mask`."""
    import sys
    args = list(argv if argv is not None else sys.argv[1:])
    if args and args[0] in ("-h", "--help"):
        print(
            "Usage: python -m axio_common.insole_sensor_mask [MASK]\n\n"
            "Decode an insole sensor-mask hex code into the set of present\n"
            "sensors and IMU state. MASK can be a bare hex (7ffd), a hex with\n"
            "the `m` marker (m7ffd), or a full filename suffix (m7ffd_ag).\n"
            "If MASK is omitted, the script reads from an interactive prompt.\n\n"
            "See axio-common/INSOLE_SENSOR_MASK.md for the format spec."
        )
        return 0
    if args:
        raw = args[0]
    else:
        try:
            raw = input("Mask (e.g. 7ffd, m7ffd_ag, 0x0007): ").strip()
        except EOFError:
            return 1
    try:
        mask, accel, gyro = _parse_cli_input(raw)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    _print_decoded(mask, accel, gyro)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli_main())
