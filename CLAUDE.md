# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

`axio-common` is the shared SQLAlchemy ORM + DB/storage config library for the **FlyNNServer** system — the
neural-network-job-distribution and calibration-monitoring platform that runs on Fly.io (Postgres + Tigris object
storage). It is a backend-agnostic core (no FastAPI, no Streamlit) holding the single source of truth for the
database schema, the calibration-procedure registry, and the Tigris storage layout. It is consumed by its two
siblings — `../axio-server` (the FastAPI API + job queue) and `../axio-web` (the dashboard) — as an **editable git
dependency pinned to a tagged version** (currently `v0.33.0`). Because both downstreams pin a tag, **all schema and
shared-logic changes are upstream-first: change, test, and tag here, then bump the pin downstream.** See the
workspace root `../CLAUDE.md` for cross-repo conventions and the upstream-first rule.

Sibling repos: `../axio-server` (API/queue/storage router), `../axio-web` (dashboard).

## Commands

This is a pure library (no app entry point, no server). It is installed editable into each consumer's conda env.

```bash
pip install -e /path/to/axio-common          # editable install (local dev)
pip install -e git+https://github.com/Axioforce/axio-common.git#egg=axio_common   # from GitHub

pytest                                        # run the procedure-registry test suite (axio_common/tests/)
pytest -k procedure_resolution                # run a single test module

bump-my-version bump patch                    # bump version (also: minor / major) — commits + tags via .bumpversion.toml
```

`update_axio_common.sh` automates the full release: prompts for a bump type, bumps + commits + tags, then
reinstalls the new version into the `axio-server` and `axio-dash` conda envs.

- **Package name**: `axio_common` (pyproject `[project]`); current version **0.33.0**.
- **Build**: setuptools/wheel; packages auto-discovered (`tool.setuptools.packages.find`).
- **Deps**: SQLAlchemy>=2.0, pydantic>=2.0, psycopg2-binary, boto3>=1.34, dotenv.

## Versioning & Release (upstream-first)

- Single version of truth: `pyproject.toml` `version`, mirrored in `.bumpversion.toml`. `bump-my-version` commits
  and creates a `vX.Y.Z` git tag in one step.
- `axio-server` (and `axio-web`) pin a **specific tag** in their requirements. Workflow for any schema or shared-logic
  change: edit + `pytest` here → `bump-my-version bump <part>` → push tag → update the pin in the downstream repo.
- Never make a downstream-only patch that diverges the schema; the divergence will be lost on the next reinstall.
- The database schema itself is **managed externally** — `axio-server` owns the Alembic migrations. There is **no
  Alembic in this repo**. `db_core` calls `Base.metadata.create_all()` only as a best-effort safety net for fresh
  local dev (it warns, not crashes, if the DB is unreachable at import).

## Architecture

### Package layout (`axio_common/`)

- `database/db_core.py` — `Base` (declarative), `engine`, `SessionLocal`, and the `get_db()` FastAPI-style
  session-dependency generator. Engine reads `DATABASE_URL` from env, uses `pool_pre_ping`, a 10-min `pool_recycle`,
  and psycopg2 TCP keepalives tuned to survive Fly.io/managed-Postgres idle-kill windows. `database/__init__.py`
  re-exports everything (so `from axio_common.database import Base, get_db`).
- `models/` — all ORM entities (below); `models/__init__.py` is the canonical export surface and also exports
  status/kind constants and parse/normalize helpers.
- `storage/` — Tigris (S3-compatible) calibration-data layer + the procedure-resolution helpers.
- `utils/` — `shared.py` (client/heartbeat registration helpers operating on a `Session`), `model_utils.current_time`
  (UTC-aware default for timestamp columns), `db_middleware.py`, `database.py`.
- `logger/` — `logger` plus a `HostnameFilter` that stamps hostname/IP onto every record.
- `insole_sensor_mask.py` / `INSOLE_SENSOR_MASK.md` — insole sensor-mask reference data.
- `tests/` — unit tests for the procedure registry (resolution, seed derivation, snapshot, validation).

### ORM model graph

The schema models the hardware-to-delivery lifecycle plus the NN job queue. Conceptual chain:
**mold → load cell → force plate → device → job → run**, with deliveries as the terminal node.

- **`LoadCell`** (`load_cells`, PK `load_cell_id`) — sensor catalog. `mold_id` is the prefix before the last dot of
  the id (`parse_load_cell_id` splits id → `(mold_id, batch_id)`); **`Mold` is a logical grouping, not its own table**
  (`MoldNote` holds per-mold notes by `mold_id`). Children: `LoadCellManufacturing` (1:1 gluing/mag-offset
  provenance), `LoadCellNote`, `Baseline`→`BaselineSensor` (per-cell baseline captures).
- **`ForcePlate`** (`force_plates`, PK `device_axf_id`) — four corner load-cell slots
  (front/rear × left/right, `CORNER_POSITIONS`). History tables `ForcePlateAssignmentHistory` (cell
  reassignments) and `ForcePlateAssembledDateHistory`. Children `AssembledBaseline`→`AssembledBaselineSensor`
  (`ASSEMBLED_BASELINE_KINDS`) hold the assembled-plate baseline.
- **`Device`** (`devices`, PK `axf_id`) — the trainable unit; `type_id`/`type_name`, best-force/best-moment run
  pointers + metrics (JSON), anomaly counters. One Device → many `Job`s.
- **`Job`** (`jobs`, PK uuid) → **`Run`** (`runs`) — the NN training job queue. Job carries `status`
  (`queued`→assigned→…), `priority`, `failure_count`, `allowed_hostnames` (targeted-daemon array), heartbeat and
  lifecycle timestamps. Assignment order is `priority DESC, queued_at ASC` (composite indexes defined on the model).
  `Run` cascades delete from `Job`.
- **`CalibrationBucketSession`** (`calibration_bucket_sessions`) → `CalibrationBucketFile` — one row per
  `<type>/<device>/<date>` bucket session synced from Tigris, with `KIND_TRAIN`/`KIND_TEST`/`KIND_OTHER` files;
  links to `Device` and to a `Calibrator`. `JobBucketSession` is the Job↔BucketSession M2M
  (`KIND_TRAIN_LINK`/`KIND_TEST_LINK`/`KIND_BOTH_LINK`).
- **`Calibrator`** (`calibrators`) — one row per person seen as `calibrated_by`; `name_key` is the unique
  lowercased key (`normalize_calibrator_name` collapses multi-author strings to the first name for now).
- **`CalibrationSession`** + `CalibrationSessionDate` + `CalibrationSessionCalibrator`
  (`CALIBRATION_SESSION_STATUSES`) — the human-scheduled calibration event, optionally linked to a `Job`.
- **`LiveTestSession`** → `LiveTestCell` / `LiveTestAggregate` (`STAGE_TYPES`, `STAGE_LOCATIONS`, `COLOR_BINS`)
  + `LiveTestSettings` — live-test monitoring.
- **`Delivery`** (`deliveries`, `DELIVERY_STATUSES`) — terminal node, links a `Device` (and optionally a `Job`).
- **`Client`** (`clients`) — registered daemon/client hosts (heartbeat tracking via `utils/shared.py`).

Most models also define paired pydantic `*Request`/`*Response` schemas in the same file for the API to reuse.

### Calibration-procedure registry

Two cooperating pieces define what activities a calibration day should contain:

1. **Static defaults** — `storage/activities.py`: the canonical `ACTIVITIES` catalog (train `TR-*` / test `TE-*`
   codes + descriptions), `TYPE_ID_TO_FAMILY` (plate families `lite`/`lp`/`xl`/`insole`),
   `DEFAULT_BY_TYPE_AND_SESSION`, and helpers (`default_expected_for`, `parse_activity_from_key`,
   `family_for_device_id`). The fallback when nothing is overridden.
2. **Editable DB registry** — `models/calibration_procedure.py`: a three-level override cascade —
   `CalibrationActivity` (global catalog) → `CalibrationFamilyActivity` (per-family grid/duration/description/tags)
   → `CalibrationActivityDayOverride` (per-day membership + overrides), with `CalibrationFamilyDay` carrying each
   day's `reverse_order` flag. Resolution is pure (no DB) in `storage/procedure_resolution.py`:
   `resolve_description` (day→family→base, most-specific non-blank wins), `accumulate_tags` (catalog+family+day,
   deduped), `derive_day_sequence`, `derive_seed_for_family`, `validate_family_put`, `snapshot_for_session`.

### Storage layer (`storage/storage_core.py`)

Calibration data lives in a Tigris bucket: `<type>/<device_id>/<iso-date>/{train,test}/<file>.csv.gz` + `tests.txt`,
models under `<type>/<device_id>/models/`. Two auto-selected backends: **S3/boto3 direct** when
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` are set; **server-mediated** (presigned URLs from `axio-server`,
`AXIO_STORAGE_TOKEN` bearer) otherwise — keeping the bucket secret off daemon machines. Override with
`AXIO_STORAGE_BACKEND=s3|server`. `storage/picker.py` provides Tk bucket-browser dialogs (opt-in; needs tkinter).

### Key environment variables

`DATABASE_URL` (Postgres); `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3`
(default Tigris), `BUCKET_NAME` (default `axioforce-calibration`); `AXIO_SERVER_URL`, `AXIO_STORAGE_TOKEN`,
`AXIO_STORAGE_BACKEND`.
