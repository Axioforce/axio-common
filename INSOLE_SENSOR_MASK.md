# Insole sensor-mask filename convention

Insoles develop arbitrary sensor failures over time. To make sure every
data file and every trained model carries an exact record of which
sensors were live when it was produced, every file name is suffixed with
a compact hex code that pins down:

1. Which of the 15 pressure (magnetometer) sensors were present, and
2. Whether the IMU's accelerometer and gyroscope channels are included.

The encoding covers every one of the 2¹⁵ × 2 × 2 = 131,072 possible
combinations without a lookup table, so an arbitrary failure pattern can
be encoded the moment it's discovered — no registry to update, no
release to cut.

This convention is the contract between:

- The **auto-detection routine** that scans incoming insole CSVs and
  decides which sensors are dead. It renames each file to carry the
  matching suffix.
- The **training pipeline** (`axio-server` Submit page →
  `AxioforceNeuralizer/NeuralNet.py`) which trains a model with a
  matching sensor subset and stamps the model output with the same
  suffix.
- The **live inference GUI** (`AxioforceNeuralizer/RunNNLiteGUI.py`)
  which reads the suffix off each new CSV and picks the trained model
  whose suffix matches.

Everyone reads and writes through the single helper module
`axio_common.insole_sensor_mask`. Don't reinvent the bit math anywhere
else — the day someone uses `1 << s` instead of `1 << (s-1)` is the day
every model labeled `m0001` actually means sensor 2.

## Format

```
<freeform-stem>_m<HEX>[_<imu>].<ext>
```

| Piece | Meaning |
|-------|---------|
| `<freeform-stem>` | Anything you want — date, activity code, device id, job id. The parser only looks at the tail. |
| `_m`              | Literal marker introducing the mask. Always lowercase `m`. |
| `<HEX>`           | Exactly 4 lowercase hex digits, zero-padded. Bit `(i-1)` is sensor `i`. `7fff` = all 15 present, `0001` = only sensor 1, `7ffd` = sensor 2 dropped, `0000` = all pressure sensors dead. |
| `_<imu>`          | Optional. `_ag` = accel + gyro, `_a` = accel only, `_g` = gyro only. Omitted entirely when neither is present. |
| `<ext>`           | The file extension (`.csv`, `.h5`, `.keras`, …). |

### Examples

| Filename | Mask | Sensors present | Accel | Gyro |
|----------|------|-----------------|-------|------|
| `0a-00000005_2026-05-21_TR-BER_m7fff_ag.csv` | `0x7fff` | 1..15 | yes | yes |
| `0a-00000005_2026-05-21_TR-BER_m7ffd_ag.csv` | `0x7ffd` | 1, 3..15 (sensor 2 dead) | yes | yes |
| `best-model_m0007_a.h5`                      | `0x0007` | 1, 2, 3 | yes | no |
| `legacy-file.csv`                            | — | unknown — treat as `m7fff_ag` (full, see "Legacy files" below) | | |

### Bit layout (single source of truth)

```
bit:    14 13 12 11 10  9  8  7  6  5  4  3  2  1  0
sensor: 15 14 13 12 11 10  9  8  7  6  5  4  3  2  1
```

Sensor 16 is the "control mag" and is **never** part of the trained
feature set. It has no bit. Don't include it in the mask.

## Installing `axio-common`

`axio_common` is the shared Python package that exposes the helpers
described below. Two install paths depending on whether you're doing
day-to-day development against a local clone or running pinned in a
deployed service:

**Local development clone (recommended while iterating):**

```bash
git clone https://github.com/Axioforce/axio-common.git
cd axio-common
pip install -e .
```

The `-e` flag installs in editable mode — pulling new commits with
`git pull` immediately makes them visible to anything that imports
`axio_common`, no reinstall needed.

**Pinned install (CI, deployed services, anything reproducible):**

```bash
pip install "axio_common @ git+https://github.com/Axioforce/axio-common.git@v0.30.1"
```

Replace `v0.30.1` with whichever tag you want to pin to. The deployed
services (`axio-server`, `axio-dash`, the daemon) all pin a specific
tag in their `requirements.txt` and bump it deliberately on each
release. See `axio-server/requirements.txt` for the current canonical
pin.

**Verify it's working:**

```bash
python -c "from axio_common.insole_sensor_mask import features_from_filename; \
           print(features_from_filename('test_m7ffd_ag.csv'))"
```

Should print a populated `InsoleFeatures(...)`. If you get
`ModuleNotFoundError`, you're either in the wrong virtualenv or the
install didn't pick up the new module — re-run `pip install -e .` from
the `axio-common` checkout.

## API

All public helpers live in `axio_common.insole_sensor_mask`:

```python
from axio_common.insole_sensor_mask import (
    encode_filename_suffix,   # -> "_m7ffd_ag"
    parse_filename_suffix,    # -> InsoleFeatures(...) or None  (strict)
    features_from_filename,   # -> InsoleFeatures (lenient — full set on legacy)
    stamp_filename,           # apply suffix before the extension
    sensors_to_mask,          # int   <- list[int]
    mask_to_sensors,          # tuple <- int
    feature_columns_for,      # ordered NN column names for a spec
    features_from_config,     # resolve from a job-config dict
    InsoleFeatures,           # dataclass(sensors, include_accel, include_gyro)
    FULL_INSOLE_FEATURES,     # InsoleFeatures with every sensor + IMU
    ALL_SENSORS_MASK,         # 0x7fff
    PRESSURE_BITS,            # 15
)
```

### Encoder

```python
encode_filename_suffix(
    sensors=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    include_accel=True,
    include_gyro=True,
)  # -> "_m7fff_ag"
```

Sensors outside `1..15` raise `ValueError`. Duplicates and order do not
matter — the encoder normalizes both.

### Decoder

Almost every consumer wants the lenient form, which applies the
project-wide "no suffix means everything on" policy automatically:

```python
features = features_from_filename("session_2026-05-21_TR-BER_m7ffd_ag.csv")
# InsoleFeatures(sensors=(1, 3, 4, ..., 15), include_accel=True, include_gyro=True)

features = features_from_filename("legacy-file-with-no-suffix.csv")
# InsoleFeatures(sensors=(1..15), include_accel=True, include_gyro=True)
# i.e. FULL_INSOLE_FEATURES

columns = feature_columns_for(features)
# ['sensor-1-x', 'sensor-3-x', ..., 'sensor-15-z', 'Ax', 'Ay', 'Az', 'Gx', 'Gy', 'Gz']
```

If you specifically need to distinguish "explicitly stamped file" from
"legacy file we're assuming things about", use the strict form:

```python
features = parse_filename_suffix("legacy-file.csv")
if features is None:
    # Explicitly handle the legacy case — e.g. log, audit, reject.
    ...
```

Both parsers are anchored to the end of the stem, so an `m<hex>` that
happens to appear mid-name (e.g. `m7fff_extra_stuff_v2.csv`) is
correctly ignored.

### Stamper

```python
stamp_filename(
    "TR-BER_2026-05-21.csv",
    sensors=[1, 2, 3],
    include_accel=True,
    include_gyro=False,
)  # -> "TR-BER_2026-05-21_m0007_a.csv"

# Idempotent — calling stamp on an already-stamped file REPLACES the suffix:
stamp_filename(
    "TR-BER_2026-05-21_m7fff_ag.csv",
    sensors=[1, 2, 3],
    include_accel=True,
    include_gyro=False,
)  # -> "TR-BER_2026-05-21_m0007_a.csv"
```

Use `stamp_filename` rather than concatenating manually — it strips any
existing suffix first, so you can't accidentally pile suffixes on top of
each other.

## Auto-detection routine — recommended flow

For the offline routine that scans incoming insole CSVs and renames them:

```python
import pandas as pd
from pathlib import Path
from axio_common.insole_sensor_mask import (
    InsoleFeatures, encode_filename_suffix, stamp_filename, PRESSURE_BITS,
)

def detect_live_sensors(csv_path: Path) -> set[int]:
    """Return the set of pressure-sensor IDs (1..15) that appear to be
    producing real data in this file. Implement whatever heuristic you
    want here — variance threshold, range check, NaN rate, etc."""
    df = pd.read_csv(csv_path)
    live = set()
    for i in range(1, PRESSURE_BITS + 1):
        cols = [f"sensor-{i}-x", f"sensor-{i}-y", f"sensor-{i}-z"]
        if not all(c in df.columns for c in cols):
            continue
        # Example heuristic: at least one axis has non-trivial variance.
        if any(df[c].std() > 1e-3 for c in cols):
            live.add(i)
    return live

def detect_imu(csv_path: Path) -> tuple[bool, bool]:
    df = pd.read_csv(csv_path, nrows=0)
    has_accel = all(c in df.columns for c in ("Ax", "Ay", "Az"))
    has_gyro = all(c in df.columns for c in ("Gx", "Gy", "Gz"))
    return has_accel, has_gyro

def rename_with_suffix(csv_path: Path) -> Path:
    live = detect_live_sensors(csv_path)
    accel, gyro = detect_imu(csv_path)
    new_name = stamp_filename(csv_path.name,
                              sensors=sorted(live),
                              include_accel=accel,
                              include_gyro=gyro)
    new_path = csv_path.with_name(new_name)
    csv_path.rename(new_path)
    return new_path
```

Two rules to hold to:

- **Idempotent renames.** `stamp_filename` strips any existing suffix
  before applying the new one, so running the routine twice on the same
  file is safe. If your detector ever changes its mind about a sensor's
  liveness, the file gets renamed; nothing breaks.
- **Don't write the suffix yourself.** Always go through
  `encode_filename_suffix` or `stamp_filename`. If we ever change the
  format (we won't, but the option exists), every consumer updates in
  one place.

## Legacy files

CSVs and models that predate this convention have no `_m<hex>` suffix.
**The project-wide policy is:** if no suffix is present, treat the file
as `m7fff_ag` — every pressure sensor live, accelerometer and gyroscope
both included. That's what those files actually are: they were captured
before any insole had dropped a sensor, on training rigs where the
whole feature set was always available.

This policy is enforced in code, not left to each consumer to
re-implement:

```python
from axio_common.insole_sensor_mask import features_from_filename

# Returns FULL_INSOLE_FEATURES for any unsuffixed file.
features = features_from_filename(some_csv_path)
```

If a specific consumer needs to *detect* legacy files (e.g. to log an
audit warning or to mark them for re-stamping), use the strict
`parse_filename_suffix` and check for `None` — but then still fall back
to `FULL_INSOLE_FEATURES` for the actual processing. Don't invent a
different fallback.

## Why hex, why 4 chars, why this format

- **4 hex chars** is the smallest fixed-width encoding that covers 15
  bits without padding ambiguity. Variable-width formats (`_s1-2-3`)
  break down past five or six sensors and end up running past filesystem
  path limits.
- **Hex** beats base32/base36 because (a) the savings are 1 char and not
  worth the readability loss, and (b) bit-flipping a hex digit to debug
  is trivial — `7fff` → `7ffe` obviously turns sensor 1 off. Base32
  doesn't have that.
- **Lowercase only**. Windows is case-insensitive, Linux and Tigris are
  case-sensitive. `M7FFD.csv` and `m7ffd.csv` are the same file on one
  filesystem and different files on the other — a great way to lose a
  model. Encoder always emits lowercase; decoder requires lowercase.
- **IMU suffix is separate**. The pressure mask is `m<hex>`; the IMU
  state is its own short tag. Keeping them separate means a future
  change to the IMU encoding (say, adding a magnetometer channel)
  doesn't require new versions of every existing mask file.

## Edge cases

- **`m0000`** is valid encoding-wise (no pressure sensors live).
  Probably useless for inference, but encodable so you can record that
  an insole is fully dead. The trainer should refuse `m0000_` (i.e. no
  IMU either) — that's a zero-feature model.
- **Mid-name `m<hex>` collisions.** The regex `_m([0-9a-f]{4})(_(?:a|g|ag))?$`
  is anchored to end-of-stem, so an unrelated `m<hex>` earlier in the
  name (e.g. `device_m1234_session_m7fff.csv`) is correctly ignored —
  only the trailing match counts.
- **Extension parsing.** Use `pathlib.Path(...).stem` (or just let
  `parse_filename_suffix` do it). Don't string-split on `.` — files like
  `model.v2.h5` would break.

## Quick mask lookup CLI

When you've got a hex code in front of you (e.g. off a renamed file or a
model filename) and just want to know which sensors it represents,
`axio_common.insole_sensor_mask` is runnable as a module:

```
$ python -m axio_common.insole_sensor_mask 7ffd
Mask:            0x7ffd  (binary 111111111111101)
Sensors present: 1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15  [14/15]
Sensors missing: 2  [1/15]
IMU:             unspecified (add an _ag / _a / _g tail to specify)
```

Pass a full suffix to also resolve the IMU state and get the canonical
form back:

```
$ python -m axio_common.insole_sensor_mask m7ffd_ag
Mask:            0x7ffd  (binary 111111111111101)
Sensors present: 1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15  [14/15]
Sensors missing: 2  [1/15]
Accelerometer:   on
Gyroscope:       on
Feature count:   48  (14 sensors x 3 + 3 accel + 3 gyro)
Canonical suffix: _m7ffd_ag
```

Accepted input forms: `7ffd`, `0x7ffd`, `m7ffd`, `m7ffd_ag`, `_m7ffd_a`.
Run with no argument to drop into an interactive prompt.

## Smoke test

```python
from axio_common.insole_sensor_mask import (
    encode_filename_suffix, parse_filename_suffix, features_from_filename,
    FULL_INSOLE_FEATURES, InsoleFeatures,
)

# Round-trip an arbitrary combination
suffix = encode_filename_suffix([1, 2, 3, 5, 8, 13],
                                include_accel=True, include_gyro=False)
assert suffix == "_m10a7_a"
parsed = parse_filename_suffix(f"anything{suffix}.csv")
assert parsed == InsoleFeatures(sensors=(1, 2, 3, 5, 8, 13),
                                 include_accel=True, include_gyro=False)

# Strict parse: explicit None for legacy files
assert parse_filename_suffix("no-suffix.csv") is None

# Lenient parse: full features for legacy files (project policy)
assert features_from_filename("no-suffix.csv") == FULL_INSOLE_FEATURES
```

The module also has doctests covering every public function. Run them
with:

```
python -m doctest axio_common/insole_sensor_mask.py -v
```

## Versioning

The encoding is fixed. There's no version field in the suffix because
there will not be a v2. If a new piece of metadata ever needs to ride
along with a file, it gets its own segment (e.g. `_m7fff_ag_t2`, with a
matching parser) rather than redefining what `m<hex>` means.

## Questions

Ping Stephen (stephen.houston@axioforce.com).
