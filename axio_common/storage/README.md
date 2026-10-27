# Calibration cache

`axio_common.storage` is a thin layer over the `axioforce-calibration` Tigris
bucket plus a **local read-through cache** that all calibration tooling
shares — DAQ, the NN training daemon, ad-hoc analysis scripts.

The cache is the authoritative working surface; the bucket is the durable,
shared backing store.

## Where

| Env var | Default | Purpose |
|---|---|---|
| `AXIO_CALIBRATION_CACHE` | `~/.axio-cache` | cache root |
| `AXIO_CALIBRATION_CACHE_MAX_GB` | `50` | GC hard cap |
| `AXIO_CALIBRATION_CACHE_MAX_AGE_DAYS` | `28` | GC TTL |
| `AXIO_CALIBRATION_CACHE_GC_INTERVAL_HOURS` | `12` | GC throttle window |
| `AXIO_CALIBRATION_CACHE_GC` | `1` | GC master switch (`0`/`false`/`off` disables) |

Shared across processes on the same machine. Captured by `storage_core` at
import time — set it before importing `axio_common.storage` if you need to
override.

## Layout

The cache mirrors the bucket with three transforms so the existing
AxioforceNeuralizer code (date parsers, `gather_calibration_data`,
`Config.split_directories_at`, model loaders) keeps working unchanged:

| Bucket | Cache |
|---|---|
| ISO dates (`2024-12-20`) | dotted (`12.20.2024`) |
| `<date>/{train,test}/...` | `<date>/calibration_data/{train,test}/...` |
| `*.csv.gz` (gzipped) | `*.csv` (decompressed on download) |

Resulting layout:

```
<cache>/<type>/<device_id>/<dotted-date>/calibration_data/train/*.csv
<cache>/<type>/<device_id>/<dotted-date>/calibration_data/test/*.csv
<cache>/<type>/<device_id>/<dotted-date>/tests.txt
<cache>/<type>/<device_id>/models/<compound-name>/...
```

`models/` is mirrored verbatim — no date or extension transforms.

Characterization-grid captures (v0.69.0) keep their bucket shape, dotted date and gunzipped:

```
<cache>/<type>/<device_id>/axiosaurus_characterization/<dotted-date>/<capture>.csv
<cache>/<type>/<device_id>/axiosaurus_characterization/<dotted-date>/axiosaurus/<run>.{json,gcode}
```

They are not session files: a job names them (Neuralizer `DEVKIT_GRID_FILES`) and
`download_grid_captures()` fetches them with every sidecar of the device.

### Mapping a cache path back to a bucket key (recovery / backfill)

`cache_path_for_key()` maps bucket→cache; its exact inverse is
`bucket_key_for_cache_path()` (cache→bucket: dotted date→ISO, drop the
`calibration_data/` layer, restore `.csv.gz`). **Always use it — never upload a
cache path verbatim as a bucket key.** A raw cache path carries the dotted date
and the cache-only `calibration_data/` segment; those keys are non-canonical and
`bucket_sync` **skips** them (it only indexes ISO date dirs), so the session
silently disappears from the dashboard/Submit page even though bytes are in the
bucket. This is exactly the 2026-07 restore incident (devices 15/16).

Guardrail: `upload_file()`, `upload_bytes()` and `presigned_put_url()` call `_assert_canonical_key()`,
which **rejects** an input-session key containing `calibration_data/` or a
non-ISO date directory — so a hand-rolled raw upload now fails loudly instead of
corrupting the index. To recover cached calibration data into the bucket, run
`AxioforceNeuralizer/sync_cache_to_tigris.py` (which uses the shared inverse), or
call `bucket_key_for_cache_path()` yourself — never a verbatim cache-path PUT.

## Public API only — the underscored helpers are not importable

`axio_common/storage/__init__.py` re-exports this module with
`from .storage_core import *`, and `import *` **skips underscore-prefixed
names**. `storage._client_singleton()` and `storage._bucket()` therefore do not
exist on the `axio_common.storage` namespace: reaching for them from another
package raises `AttributeError` at call time, not import time, so the failure
only shows up in production on the first request that needs it (that is exactly
how axio-server's packing-list endpoint broke). Upload in-memory bodies with
`upload_bytes(key, data, content_type=...)` and files with `upload_file()`.

## End-to-end flow

The cache plays two roles at once: **upload staging** for the DAQ side,
and **download cache** for the daemon side. On a single capture-and-train
machine those roles collapse into one — the daemon's `ensure_local` finds
every file already present from the DAQ write and skips the download.

```
DAQ                                                        NN daemon
 │                                                          │
 │ 1. write CSVs into cache (legacy layout, plain .csv)     │
 │                                                          │
 │ 2. storage.upload_session_files(...) — gzipped on wire   │
 │ ───────────────────────────────────► bucket              │
 │                                                          │
 │                                       3. job submitted   │
 │                                          to NN server    │
 │                                                          │
 │                                       4. daemon picks    │
 │                                          up the job ◄────┤
 │                                                          │
 │                                       5. for each key,   │
 │                                          ensure_local(): │
 │                                            cache hit  → no-op
 │                                            cache miss → bucket → cache
 │                                                          │
 │                                       6. training reads  │
 │                                          cache paths;    │
 │                                          writes models   │
 │                                          + nn_results to │
 │                                          cache           │
 │                                                          │
 │                                       7. daemon uploads  │
 │                                          model dir to    │
 │                                          <device>/models/│
 │                                          ─────► bucket   │
```

### DAQ side

Two reasons the DAQ writes to the cache instead of an arbitrary scratch dir:

1. **Staging for upload.** `upload_session_files()` reads from local paths and
   gzips on the wire; the file has to live somewhere first.
2. **Local persistence for re-use.** The same desktop is usually the training
   host. If the file is already in the cache when the daemon picks up the
   matching job, no download happens.

The DAQ does **not** need to know about ISO/dotted dates, gzip, or the
`calibration_data/` injection. It needs one thing: the local directory it
should write its CSVs into. Resolve it via the public helpers (cf.
`device_type` + `_dotted_from_iso`) — or use the `local_session_dir()`
helper once it lands (see _Open items_).

### NN daemon side

Receives a job referring to `(device_id, iso_date)`. Calls
`storage.download_session(device_id, iso_date)` and gets back a local Path
ready for the existing `gather_calibration_data()` walker. The dotted-date
folder, the `calibration_data/{train,test}` substructure, and plain `.csv`
files are all set up by the cache layer.

### Models / training output

Training writes `best_model/` and `nn_results/` into the same session dir
the inputs came from. At job end, the daemon uploads the model artifacts to
`<type>/<device_id>/models/<compound-name>/...`. Other machines that later
need the same model fall through `ensure_local` and pull it down from the
bucket.

(Future: per the deferred plan, new training runs will write to
`<device>/models/<job_uuid>/...` keyed off a Job/Run record in Postgres
rather than the compound-date convention. Compound dirs stay readable as
legacy artifacts.)

## What the cache is _not_

- **Not authoritative, but not self-refreshing either.** A cache hit never
  asks the bucket whether the object changed. An object overwritten under the
  same key is only picked up if the key is listed in the bucket's
  invalidation manifest (next section).
- **Garbage-collected (`cache_gc.py`).** `ensure_local`/`upload_file` trigger a
  throttled, background sweep: evict whole sessions/model-dirs past the TTL
  (`MAX_AGE_DAYS`), then oldest-first until under `MAX_GB`. **A file is deletable
  only if it carries a fresh *verification sidecar*** — a hidden marker written
  the instant a confirmed upload/download completes. An un-uploaded capture, or a
  re-capture that overwrote the file after its last upload, has no fresh sidecar
  and is never touched (it's reported as un-verified and left in place). This is
  a local, content-honest proof — stronger than "a key with this name exists in
  the bucket", which collides on the deterministic capture filenames. Recency is
  keyed off mtime (NTFS atime is off by default on Windows); cache-hit reads call
  `mark_used` to re-stamp file + sidecar so an actively-used session stays
  protected. Caveats: a cache populated before this shipped is GC-inert until its
  files are next accessed (safe — under-evicts), and server-side bucket deletion
  is not detected (the calibration bucket is treated as append-only). The module
  is mirrored byte-for-byte into `AxioforceDynamoPy/app/storage/cache_gc.py` —
  keep both copies identical.
- **Not a sync target.** It is populated lazily, per-key, on read. There is
  no `sync_all()` and no background refresh.

## Rewriting objects: the invalidation manifest

Objects are normally write-once. When one has to be **overwritten under the
same key** (e.g. a capture re-derived with a fix), every machine that cached
the old bytes would keep using them, so the bucket carries a manifest naming
the rewritten keys:

```
_config/cache_invalidations.json
{"version": 1,
 "entries": {"<bucket key>": {"etag": "<ETag of the CURRENT object>", "reason": "...", "at": "<iso>"}}}
```

- Every download records the object's bucket ETag in the file's verification
  sidecar (`{"etag": "..."}`; empty = unknown, as in legacy sidecars and after
  an upload). The sidecar's mtime rules for GC are unchanged.
- On a cache hit for a key listed in the manifest, `ensure_local` re-downloads
  when the recorded ETag differs from the manifest's (or is unknown). The new
  copy replaces the old one atomically; if the download fails it raises and the
  old copy stays. All other entry points (`download_files`,
  `download_session`, the pickers) go through `ensure_local`.
- **An un-verified local file is never replaced** (no sidecar, or one older
  than the file): it may be an un-uploaded capture. A warning is logged once.
- **Cost:** the manifest is fetched lazily, at most once per 10 minutes per
  process, on either backend (direct `get_object`, or a presigned GET via
  axio-server). A hit on an unlisted key makes no request. A missing manifest
  is empty; any other fetch error is logged once and treated as empty (fail
  open, so a bucket hiccup never breaks a cache hit).

Operator flow, after overwriting the objects (needs direct bucket creds):

```
python -m axio_common.storage.invalidate <key> [<key> ...] --reason "..." [--dry-run]
```

It HEADs each key for its current ETag and merges the entries into the manifest
(existing entries kept, same key replaced; a missing key aborts with nothing
written). **If an object is rewritten again later, re-run it for that key**: a
stale manifest ETag makes every hit on that key re-download (logged once).
Don't run two at once; the read-modify-write is not atomic.

## Open items

- `local_session_dir(device_id, iso_date)` helper so the DAQ and helper
  scripts can resolve the cache path without poking at private functions.
- ~~LRU eviction by total bytes~~ — done, see `cache_gc.py`.
- ~~ETag-aware staleness check~~ — done for rewritten keys listed in the
  invalidation manifest (see above); unlisted keys are still never re-checked.
- File picker widget (Tk) for helper scripts that today use
  `filedialog.askopenfilenames` against local paths.
