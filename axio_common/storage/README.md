# Calibration cache

`axio_common.storage` is a thin layer over the `axioforce-calibration` Tigris
bucket plus a **local read-through cache** that all calibration tooling
shares — DAQ, the NN training daemon, ad-hoc analysis scripts.

The cache is the authoritative working surface; the bucket is the durable,
shared backing store.

## Where

| Env var | Default |
|---|---|
| `AXIO_CALIBRATION_CACHE` | `~/.axio-cache` |

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

- **Not authoritative.** The bucket wins on conflict. If `ensure_local`
  finds a stale local copy, today it returns the stale copy — there is no
  ETag check yet (see _Open items_).
- **Not garbage-collected.** Grows monotonically; clean manually with
  `rm -rf $AXIO_CALIBRATION_CACHE` or by deleting individual session dirs.
  LRU eviction by total bytes is on the roadmap.
- **Not a sync target.** It is populated lazily, per-key, on read. There is
  no `sync_all()` and no background refresh.

## Open items

- `local_session_dir(device_id, iso_date)` helper so the DAQ and helper
  scripts can resolve the cache path without poking at private functions.
- LRU eviction by total bytes (env var: `AXIO_CALIBRATION_CACHE_MAX_GB`?).
- ETag-aware staleness check in `ensure_local` for cases where the bucket
  copy is updated after the local copy was written.
- File picker widget (Tk) for helper scripts that today use
  `filedialog.askopenfilenames` against local paths.
