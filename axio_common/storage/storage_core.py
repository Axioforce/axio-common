"""
Calibration data storage on Tigris (S3-compatible).

Bucket layout (post-migration, 2026-05-06):

    <bucket>/
      <type>/<device_id>/<iso-date>/train/<filename>.csv.gz
      <type>/<device_id>/<iso-date>/test/<filename>.csv.gz
      <type>/<device_id>/<iso-date>/tests.txt
      <type>/<device_id>/models/<compound-name>/...    (training artifacts)

Where:
- type is the 2-char device type id (first segment of device_id, e.g. "10")
- device_id is the full type-id pair, e.g. "10-00000002"
- date is ISO format ("2024-12-20"); compound model dir names preserve the legacy
  dotted form ("12.20.31.2024") since they're write-once labels

CSVs are gzip-compressed in the bucket (.csv.gz extension). pandas.read_csv handles
the .gz extension transparently on the read side, so consumer code that reads CSVs
needs no change beyond using the cached .csv.gz path.

Two backends are supported, selected automatically:

  - **S3 backend** (boto3 direct): used when AWS_ACCESS_KEY_ID and
    AWS_SECRET_ACCESS_KEY are both set in the env. This is the
    high-throughput developer/server path with no extra hops.

  - **Server-mediated backend** (presigned URLs via axio-server): used when
    AWS creds are NOT set (typical of daemon machines, where we don't want
    to ship the bucket secret). List/dates/devices/sessions go to
    `<AXIO_SERVER_URL>/storage/*`; downloads use a presigned URL minted by
    axio-server, then a plain GET to Tigris (egress is free, no proxy load
    on axio-server). The bucket secret stays on axio-server only.

Override the auto-pick with AXIO_STORAGE_BACKEND=s3 or =server.

Configuration via env vars:
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY  (S3 backend; absence implies server)
    AWS_ENDPOINT_URL_S3       (default: https://fly.storage.tigris.dev)
    AWS_REGION                (default: auto)
    BUCKET_NAME               (default: axioforce-calibration)
    AXIO_SERVER_URL           (default: https://axio-server.fly.dev) — server backend
    AXIO_STORAGE_TOKEN        bearer token for axio-server's storage router
                              (server-mediated backend; sent as
                              'Authorization: Bearer <token>' on every call)
    AXIO_STORAGE_BACKEND      ("s3" | "server"; default auto)
    AXIO_CALIBRATION_CACHE    (default: ~/.axio-cache)

Cache: ensure_local() lazily downloads keys to AXIO_CALIBRATION_CACHE, mirroring the
bucket layout but inserting "calibration_data/" between the date and {train,test}
so that downstream code which walks date directories sees the OneDrive-shaped
structure it expects.

Eviction: handled by cache_gc.py. ensure_local() and upload_file() trigger a
throttled, background GC sweep that evicts whole sessions/model-dirs older than
AXIO_CALIBRATION_CACHE_MAX_AGE_DAYS (default 28) and, if still over
AXIO_CALIBRATION_CACHE_MAX_GB (default 50), oldest-first until under the cap.
A file is eligible for eviction only if it carries a verification sidecar —
written here the moment a confirmed upload or download completes — that is at
least as new as the file. An un-uploaded capture (or a re-capture that rewrote
the file after its last upload) has no fresh sidecar and is never touched. See
cache_gc.py for the policy.

Invalidation: a cache hit never re-checks the bucket, except for keys listed in
the bucket's `_config/cache_invalidations.json` manifest (objects rewritten
under the same key). See "bucket-driven cache invalidation" below and
`python -m axio_common.storage.invalidate`.

NOTE: requires boto3. Add it to axio-common's pyproject.toml dependencies before shipping.
"""
from __future__ import annotations

import gzip
import io
import json as _json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence

import boto3
from botocore.client import Config
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

DEFAULT_AXIO_SERVER_URL = "https://axio-server.fly.dev"

DEFAULT_ENDPOINT = "https://fly.storage.tigris.dev"
DEFAULT_REGION = "auto"
DEFAULT_BUCKET = "axioforce-calibration"
DEFAULT_CACHE_ROOT = Path(
    os.environ.get("AXIO_CALIBRATION_CACHE", str(Path.home() / ".axio-cache"))
)

DEVICE_ID_RE = re.compile(r"^([0-9a-fA-F]{2})-[0-9a-fA-F]+$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _dotted_from_iso(iso_date: str) -> str:
    """'2024-12-20' -> '12.20.2024'. Used to keep the local cache layout
    matching the legacy OneDrive structure that the rest of the codebase
    parses with date.split('.')."""
    y, m, d = iso_date.split("-")
    return f"{m}.{d}.{y}"


def _translate_iso_dates(key: str) -> str:
    """Replace any ISO-date path segments in `key` with dotted form."""
    return "/".join(
        _dotted_from_iso(p) if ISO_DATE_RE.match(p) else p
        for p in key.split("/")
    )


# ---------- backend selection ----------

def _axio_server_url() -> str:
    """Base URL of axio-server for the server-mediated backend."""
    url = os.environ.get("AXIO_SERVER_URL")
    return url.rstrip("/") if url else DEFAULT_AXIO_SERVER_URL


def _use_server_backend() -> bool:
    """True when read/list/download go through axio-server instead of boto3
    direct. Auto-picks based on AWS creds, overridable via AXIO_STORAGE_BACKEND."""
    explicit = os.environ.get("AXIO_STORAGE_BACKEND", "").strip().lower()
    if explicit == "server":
        return True
    if explicit == "s3":
        return False
    has_creds = bool(os.environ.get("AWS_ACCESS_KEY_ID")) and bool(
        os.environ.get("AWS_SECRET_ACCESS_KEY")
    )
    return not has_creds


def _server_auth_headers() -> dict:
    """Authorization header for axio-server's storage router. Empty when no
    token is configured — the server allows that in dev / when the env var
    is unset on its side."""
    token = os.environ.get("AXIO_STORAGE_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _server_get_json(path: str, timeout: int = 30):
    """GET <server>/path and return the parsed JSON body."""
    url = f"{_axio_server_url()}{path}"
    req = urllib.request.Request(url, headers=_server_auth_headers())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _json.loads(resp.read())


def _server_post_json(path: str, body: dict, timeout: int = 30):
    """POST JSON to <server>/path and return the parsed JSON body."""
    url = f"{_axio_server_url()}{path}"
    headers = {"Content-Type": "application/json", **_server_auth_headers()}
    req = urllib.request.Request(
        url,
        data=_json.dumps(body).encode("utf-8"),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _json.loads(resp.read())


_client = None


def _client_singleton():
    """Lazy-init boto3 client. Reuses connection pool across calls."""
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3", DEFAULT_ENDPOINT),
            region_name=os.environ.get("AWS_REGION", DEFAULT_REGION),
            # Bounded so a stalled connection FAILS instead of hanging forever:
            #   connect_timeout — cap the TCP/TLS handshake.
            #   read_timeout    — cap each socket read; a stream that goes quiet
            #                     this long is treated as dead (was botocore's
            #                     60s default, made explicit here).
            #   retries         — botocore's own bounded retry for transient
            #                     errors on the *API call* (LIST/HEAD, the
            #                     GetObject request, download_file's internals).
            #                     This does NOT cover a stall mid-body-read; that
            #                     is handled by _transfer_with_retry() below.
            config=Config(
                signature_version="s3v4",
                max_pool_connections=32,
                connect_timeout=10,
                read_timeout=60,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
    return _client


def _bucket() -> str:
    return os.environ.get("BUCKET_NAME", DEFAULT_BUCKET)


# ---------- cache garbage collection ----------

import logging as _logging
from . import cache_gc as _cache_gc

_gc_logger = _logging.getLogger(__name__)


def _maybe_run_cache_gc() -> None:
    """Throttled, fire-and-forget GC trigger for the Tigris hot path. Reads a
    stamp file and returns immediately unless a sweep is due; the sweep itself
    runs in a background daemon thread. Local-only (no bucket calls). Never
    raises."""
    _cache_gc.maybe_run_gc(DEFAULT_CACHE_ROOT, logger=_gc_logger.info)


# ---------- bucket-driven cache invalidation ----------
#
# The cache never re-checks the bucket on a hit, so an object OVERWRITTEN under
# the same key would be served stale forever by every machine that cached it.
# The bucket therefore carries a small manifest naming the keys that were
# rewritten and the ETag of their CURRENT object:
#
#   _config/cache_invalidations.json
#   {"version": 1,
#    "entries": {"<bucket key>": {"etag": "<current ETag, no quotes>",
#                                 "reason": "...", "at": "<iso>"}}}
#
# On a cache hit for a key listed there, the ETag recorded in the file's
# verification sidecar (written on every download) is compared with the
# manifest's; on a mismatch, or no recorded ETag (legacy sidecar / upload), the
# file is re-downloaded. A hit on any other key costs nothing: the manifest is
# fetched at most once per INVALIDATION_TTL_S per process, lazily on the first
# hit, and any failure to fetch it fails open (treated as empty, logged once).
# Written by the operator with `python -m axio_common.storage.invalidate`.

INVALIDATION_MANIFEST_KEY = "_config/cache_invalidations.json"
INVALIDATION_TTL_S = 600.0

_inv_lock = threading.Lock()
_inv_entries: dict[str, str] = {}      # key -> normalized current ETag
_inv_loaded_at: Optional[float] = None  # time.monotonic() of the last load attempt
_inv_error_logged = False
_inv_unverified_logged: set[str] = set()
_inv_still_mismatched_logged: set[str] = set()


def _reset_invalidation_manifest() -> None:
    """Forget the in-memory manifest (tests; forces a reload on next use)."""
    global _inv_entries, _inv_loaded_at, _inv_error_logged
    with _inv_lock:
        _inv_entries = {}
        _inv_loaded_at = None
        _inv_error_logged = False
        _inv_unverified_logged.clear()
        _inv_still_mismatched_logged.clear()


def parse_invalidation_manifest(raw: bytes | str) -> dict[str, str]:
    """{key: normalized ETag} from a manifest body. Raises ValueError on a body
    that is not a version-1 manifest; malformed entries are skipped."""
    data = _json.loads(raw)
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("cache invalidation manifest: unsupported version")
    entries = data.get("entries") or {}
    if not isinstance(entries, dict):
        raise ValueError("cache invalidation manifest: 'entries' is not an object")
    out: dict[str, str] = {}
    for k, v in entries.items():
        if isinstance(k, str) and isinstance(v, dict):
            e = _cache_gc._normalize_etag(v.get("etag"))
            if e:
                out[k] = e
    return out


def _fetch_invalidation_manifest_bytes() -> Optional[bytes]:
    """Raw manifest body, or None when the manifest object does not exist.
    Raises on any other failure."""
    if _use_server_backend():
        info = _server_post_json(
            "/storage/presigned-download",
            {"key": INVALIDATION_MANIFEST_KEY, "expires_in": 300},
        )
        try:
            with urllib.request.urlopen(info["url"], timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
    try:
        resp = _client_singleton().get_object(Bucket=_bucket(), Key=INVALIDATION_MANIFEST_KEY)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return resp["Body"].read()


def _invalidation_entries() -> dict[str, str]:
    """The manifest's {key: ETag}, loaded at most once per INVALIDATION_TTL_S.
    Never raises: a failed load is logged once and treated as empty (and is
    not retried until the TTL lapses, so a bucket outage costs one attempt per
    TTL, not one per cache hit)."""
    global _inv_entries, _inv_loaded_at, _inv_error_logged
    with _inv_lock:
        now = time.monotonic()
        if _inv_loaded_at is not None and (now - _inv_loaded_at) < INVALIDATION_TTL_S:
            return _inv_entries
        try:
            raw = _fetch_invalidation_manifest_bytes()
            _inv_entries = parse_invalidation_manifest(raw) if raw else {}
        except Exception as e:  # noqa: BLE001 -- must never break ensure_local
            _inv_entries = {}
            if not _inv_error_logged:
                _inv_error_logged = True
                _gc_logger.warning(
                    "cache invalidation manifest %s could not be loaded (%s); "
                    "treating it as empty", INVALIDATION_MANIFEST_KEY, e,
                )
        _inv_loaded_at = now
        return _inv_entries


def _cache_hit_is_stale(key: str, local: Path) -> bool:
    """True when the cached `local` copy of `key` must be re-downloaded because
    the bucket's invalidation manifest says the key was rewritten and the
    sidecar does not record the current ETag.

    Only a VERIFIED file (fresh sidecar: its content is proven to be a bucket
    copy) is ever replaced. An un-verified file may be an un-uploaded capture
    or re-capture staged in the cache; replacing it would destroy data, so it
    is kept and a warning is logged once per key."""
    want = _invalidation_entries().get(key)
    if want is None:
        return False
    if not _cache_gc.is_verified(local):
        if key not in _inv_unverified_logged:
            _inv_unverified_logged.add(key)
            _gc_logger.warning(
                "%s is listed as rewritten in the bucket, but the cached copy %s "
                "is not verified as a bucket copy (possibly an un-uploaded "
                "capture); keeping it", key, local,
            )
        return False
    return _cache_gc.read_verified_etag(local) != want


def _note_post_refresh_etag(key: str, etag: Optional[str]) -> None:
    """After an invalidation-driven re-download, warn once if the bucket's ETag
    still differs from the manifest's: the object was rewritten again after
    the manifest entry, so every hit will re-download until the entry is
    refreshed (re-run the invalidate helper)."""
    want = _inv_entries.get(key)
    got = _cache_gc._normalize_etag(etag)
    if want and got and got != want and key not in _inv_still_mismatched_logged:
        _inv_still_mismatched_logged.add(key)
        _gc_logger.warning(
            "%s: bucket ETag %s != invalidation manifest ETag %s; refresh the "
            "manifest entry or every cache hit will re-download it", key, got, want,
        )


# ---------- key construction ----------

def device_type(device_id: str) -> str:
    """'10-00000002' -> '10'."""
    m = DEVICE_ID_RE.match(device_id)
    if not m:
        raise ValueError(f"Not a valid device id: {device_id!r}")
    return m.group(1)


def device_prefix(device_id: str) -> str:
    return f"{device_type(device_id)}/{device_id}/"


def session_prefix(device_id: str, date: str) -> str:
    return f"{device_prefix(device_id)}{date}/"


def session_kind_prefix(device_id: str, date: str, kind: str) -> str:
    return f"{session_prefix(device_id, date)}{kind}/"


def models_prefix(device_id: str) -> str:
    return f"{device_prefix(device_id)}models/"


def make_key(device_id: str, date: str, kind: str, filename: str) -> str:
    """Build an object key for a session file. Filename is used verbatim — the
    .gz extension on .csv uploads is added by upload_file(), not here, so that
    presigned-URL flows (DAQ) producing the key can stay agnostic of how the
    body is encoded."""
    return f"{session_kind_prefix(device_id, date, kind)}{filename}"


# ---------- key <-> cache-path mapping ----------

# Cache mirrors the bucket but injects "calibration_data/" between the date and
# {train,test} so callers that walk a session directory see the OneDrive-shaped
# layout (./<date>/calibration_data/{train,test}/*.csv.gz). The models/ subtree
# is mirrored verbatim — no extra layer.
_INPUT_KIND_RE = re.compile(r"^(?P<head>[^/]+/[^/]+/[^/]+)/(?P<kind>train|test)/(?P<rest>.+)$")

# Characterization-grid (Axiosaurus) captures sit in their own subtree, one
# directory per capture date, with the run's g-code + sidecar JSON beside them:
#   <type>/<device>/axiosaurus_characterization/<iso-date>/<capture>.csv.gz
#   <type>/<device>/axiosaurus_characterization/<iso-date>/axiosaurus/<run>.{json,gcode}
# They are not train/test session files, so a job names them explicitly
# (Neuralizer DEVKIT_GRID_FILES, #1107).
GRID_DIR = "axiosaurus_characterization"
GRID_SIDECAR_DIR = "axiosaurus"
_GRID_CAPTURE_RE = re.compile(
    rf"^(?P<head>[^/]+/[^/]+/{GRID_DIR}/[^/]+)/(?P<name>[^/]+)\.csv\.gz$")


def cache_path_for_key(key: str, cache_root: Path | None = None) -> Path:
    """Map a bucket key to the local cache path.

    Two transforms applied so the cache mirrors the legacy OneDrive structure
    that AxioforceNeuralizer's date parsers and CSV globbers already expect:
      - ISO dates ('2024-12-20') in any path segment become dotted ('12.20.2024').
      - '.csv.gz' filenames become '.csv' (the gunzip happens in ensure_local).
    """
    root = Path(cache_root) if cache_root else DEFAULT_CACHE_ROOT
    translated = _translate_iso_dates(key)
    m = _INPUT_KIND_RE.match(translated)
    if m:
        rest = m.group("rest")
        if rest.endswith(".csv.gz"):
            rest = rest[:-3]
        return root / m.group("head") / "calibration_data" / m.group("kind") / rest
    # A grid capture is gunzipped by ensure_local like an input file, so its
    # cache name must lose '.gz' too (it used to keep it: plain CSV bytes under
    # a .csv.gz name, which pandas then fails to gunzip).
    g = _GRID_CAPTURE_RE.match(translated)
    if g:
        return root / g.group("head") / (g.group("name") + ".csv")
    return root / translated


def local_session_dir(
    device_id: str, iso_date: str, cache_root: Path | None = None,
) -> Path:
    """Local cache path for a session — where files would live if downloaded.

    No network I/O. Use this when you need the session directory shape (for
    config paths, file pickers, etc.) without paying for a download.
    """
    root = Path(cache_root) if cache_root else DEFAULT_CACHE_ROOT
    return root / device_type(device_id) / device_id / _dotted_from_iso(iso_date)


# Whole-segment dotted date ('07.27.2026'); deliberately anchored so a filename
# like '..._07.27.2026.csv.gz' (one segment with a prefix) is NOT matched.
DOTTED_DATE_RE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$")


def _iso_from_dotted(dotted: str) -> str:
    """'07.27.2026' (MM.DD.YYYY) -> '2026-07-27'. Inverse of _dotted_from_iso."""
    m, d, y = dotted.split(".")
    return f"{y}-{int(m):02d}-{int(d):02d}"


def bucket_key_for_cache_path(cache_path, cache_root: Path | None = None) -> str:
    """Map a local cache path back to its bucket key — the exact inverse of
    ``cache_path_for_key``. Reverses the cache's layout transforms:

      - the session **date directory** dotted ('07.27.2026') -> ISO
        ('2026-07-27') — only the ``<type>/<device>/<date>/`` segment, never a
        date embedded in a filename;
      - the cache-only ``calibration_data/`` layer between the date and
        {train,test} is removed;
      - a gunzipped ``.csv`` input file gets its bucket ``.csv.gz`` extension
        back.

    The ``models/`` subtree and ``tests.txt`` are mirrored verbatim (models keep
    their dotted-date dirs — that matches the bucket). **Always use this for a
    cache -> bucket upload/restore** instead of using a cache path as a key
    directly (the latter leaks 'calibration_data/' + dotted dates into the
    bucket and de-syncs the session index; `upload_file`/`presigned_put_url`
    reject such keys — see `_assert_canonical_key`).
    """
    root = Path(cache_root) if cache_root else DEFAULT_CACHE_ROOT
    p = Path(cache_path).expanduser()
    try:
        rel = p.resolve().relative_to(root.expanduser().resolve())
        parts = list(rel.as_posix().split("/"))
    except ValueError:
        # Not under the cache root — treat the input as an already-relative path.
        parts = list(Path(cache_path).as_posix().split("/"))
    # 1) session date dir (index 2): dotted -> ISO. 'models' and anything that
    #    isn't a whole-segment dotted date are left untouched, so the models
    #    subtree stays verbatim.
    if len(parts) >= 3 and DOTTED_DATE_RE.match(parts[2]):
        parts[2] = _iso_from_dotted(parts[2])
    # 2) drop the cache-only 'calibration_data/' layer for input files.
    if len(parts) >= 5 and parts[3] == "calibration_data" and parts[4] in ("train", "test"):
        del parts[3]
        # 3) restore the .csv.gz extension a gunzipped cache file lost.
        if parts[-1].endswith(".csv"):
            parts[-1] = parts[-1] + ".gz"
    # 4) grid subtree: its date dir is one level deeper, and a capture (a
    #    .csv directly in the date dir) gets its .csv.gz back.
    if len(parts) >= 5 and parts[2] == GRID_DIR:
        if DOTTED_DATE_RE.match(parts[3]):
            parts[3] = _iso_from_dotted(parts[3])
        if len(parts) == 5 and parts[4].endswith(".csv"):
            parts[4] = parts[4] + ".gz"
    return "/".join(parts)


def _assert_canonical_key(key: str) -> None:
    """Guard against uploading a **cache-shaped** path as a bucket key.

    The bucket layout is ``<type>/<device>/<iso-date>/{train,test}/<file>`` — no
    ``calibration_data/`` layer, ISO (not dotted) session dates. A cache path
    used verbatim as a key violates both and silently de-syncs the session index
    (bucket_sync skips non-ISO date dirs). Callers must map via
    ``bucket_key_for_cache_path`` first. Raises ValueError on a non-canonical
    input-session key; models/tests.txt/other prefixes are unaffected.
    """
    parts = key.split("/")
    if "calibration_data" in parts:
        raise ValueError(
            f"Refusing non-canonical bucket key (cache-only 'calibration_data/' "
            f"segment present): {key!r}. Map cache paths with "
            f"bucket_key_for_cache_path() before uploading."
        )
    # Input-session file: <type>/<device>/<date>/{train,test}/<file>
    if len(parts) >= 5 and parts[3] in ("train", "test") and not ISO_DATE_RE.match(parts[2]):
        raise ValueError(
            f"Refusing non-canonical bucket key (session date {parts[2]!r} is not "
            f"ISO YYYY-MM-DD): {key!r}. Map cache paths with "
            f"bucket_key_for_cache_path() before uploading."
        )


# ---------- listing ----------

def list_prefix(prefix: str, recursive: bool = True) -> list[str]:
    """List object keys under a prefix.
    recursive=True -> all keys under the prefix (paginated).
    recursive=False -> first level only via delimiter; CommonPrefixes are returned
                       as 'foo/' style entries alongside leaf keys."""
    s3 = _client_singleton()
    out: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    kwargs = {"Bucket": _bucket(), "Prefix": prefix}
    if not recursive:
        kwargs["Delimiter"] = "/"
    for page in paginator.paginate(**kwargs):
        for cp in page.get("CommonPrefixes", []) or []:
            out.append(cp["Prefix"])
        for obj in page.get("Contents", []):
            out.append(obj["Key"])
    return out


def list_top_dirs(prefix: str = "") -> list[str]:
    """Return just the immediate sub-directories under prefix (no files)."""
    s3 = _client_singleton()
    out: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=_bucket(), Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []) or []:
            out.append(cp["Prefix"].removeprefix(prefix).rstrip("/"))
    return sorted(out)


def list_device_types() -> list[str]:
    """Top-level type directories, e.g. ['10', '11', '12']."""
    if _use_server_backend():
        return _server_get_json("/storage/device-types")
    return list_top_dirs("")


def list_devices(device_type_id: str) -> list[str]:
    """Device ids under a type, e.g. ['10-00000002', '10-00000003', ...]."""
    if _use_server_backend():
        from urllib.parse import quote
        return _server_get_json(f"/storage/devices?type={quote(device_type_id)}")
    return list_top_dirs(f"{device_type_id}/")


def list_dates(device_id: str) -> list[str]:
    """Date strings (and 'models') under a device prefix.

    Returns ISO dates first (sorted) then 'models' if present.
    """
    if _use_server_backend():
        from urllib.parse import quote
        # Server endpoint already excludes 'models'; re-append it for parity
        # with the s3 path. (The daemon flow doesn't care, but the picker does.)
        return _server_get_json(f"/storage/sessions/{quote(device_id)}/dates")
    children = list_top_dirs(device_prefix(device_id))
    # Only session dates: a dev kit also has 'axiosaurus_characterization/',
    # which is not a session and used to be offered as one (list_grid_dates).
    dates = sorted(c for c in children if ISO_DATE_RE.match(c))
    if "models" in children:
        dates.append("models")
    return dates


def list_models(device_id: str) -> list[str]:
    """Compound-name dirs under <device>/models/."""
    return list_top_dirs(models_prefix(device_id))


@dataclass
class SessionListing:
    train: list[str]
    test: list[str]
    tests_txt: Optional[str]
    other: list[str]

    def all_keys(self) -> list[str]:
        keys = list(self.train) + list(self.test) + list(self.other)
        if self.tests_txt:
            keys.append(self.tests_txt)
        return keys


def is_folder_marker(key: str) -> bool:
    """True for a zero-byte 'directory' object — a key ending in '/'.

    Some S3 browsers create these to make an empty prefix visible. They are
    prefixes, not files, and must never reach a caller that treats a listing
    as a list of readable files: a marker under train/ becomes a *directory*
    path in a job's TRAIN_INPUT_DIR, and pandas.read_csv on a directory
    raises PermissionError (errno 13) on Windows — which the Neuralizer's
    read-retry loop reads as a cloud-sync stall and burns its whole timeout
    on before killing the job.
    """
    return key.endswith("/")


def list_session(device_id: str, date: str) -> SessionListing:
    """All keys under one (device, date) session, split by kind.

    Zero-byte folder markers are dropped — see is_folder_marker().
    """
    if _use_server_backend():
        from urllib.parse import quote
        data = _server_get_json(
            f"/storage/sessions/{quote(device_id)}/{quote(date)}"
        )
        # Filter here too: an older deployed server may not have this fix yet.
        def _clean(v):
            return sorted(k for k in (v or []) if not is_folder_marker(k))
        return SessionListing(
            train=_clean(data.get("train")),
            test=_clean(data.get("test")),
            tests_txt=data.get("tests_txt"),
            # Server doesn't surface 'other' yet; keep it empty when missing.
            other=_clean(data.get("other")),
        )
    keys = list_prefix(session_prefix(device_id, date), recursive=True)
    train, test, other = [], [], []
    tests_txt: Optional[str] = None
    base = session_prefix(device_id, date)
    for k in keys:
        if is_folder_marker(k):
            continue
        rel = k.removeprefix(base)
        if rel.startswith("train/"):
            train.append(k)
        elif rel.startswith("test/"):
            test.append(k)
        elif rel == "tests.txt":
            tests_txt = k
        else:
            other.append(k)
    return SessionListing(train=sorted(train), test=sorted(test), tests_txt=tests_txt, other=sorted(other))


def list_session_local_paths(
    device_id: str, date: str, cache_root: Path | None = None,
) -> dict[str, list[str] | Optional[str]]:
    """List the local cache paths a session WOULD have if downloaded.

    Returns {"train": [...], "test": [...], "tests_txt": str | None}.
    Each path is the eventual gunzipped/dotted-date local file path —
    i.e. cache_path_for_key applied to the bucket key.

    Useful for filling out a job config (TRAIN_INPUT_DIR / TEST_INPUT_DIR)
    on a submitter machine that doesn't actually need the file bytes — the
    daemon will download on its end.
    """
    listing = list_session(device_id, date)
    return {
        "train": [str(cache_path_for_key(k, cache_root)) for k in listing.train],
        "test": [str(cache_path_for_key(k, cache_root)) for k in listing.test],
        "tests_txt": (
            str(cache_path_for_key(listing.tests_txt, cache_root))
            if listing.tests_txt else None
        ),
    }


# ---------- download / cache ----------

def key_exists(key: str) -> bool:
    s3 = _client_singleton()
    try:
        s3.head_object(Bucket=_bucket(), Key=key)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


# Transfer-level retry. A stall mid-body-read (StreamingBody.read(), or the
# urllib GET on the server backend) raises *after* the request has returned, so
# botocore's client-level `retries=` never sees it. We retry the whole download
# here — bounded — so a transient blip recovers but a genuine outage still FAILS
# (after _TRANSFER_MAX_ATTEMPTS) rather than hanging forever or passing silently.
_TRANSFER_MAX_ATTEMPTS = 3
_TRANSFER_BACKOFF_BASE = 1.5  # seconds; exponential backoff: ~1.5s then ~3s

_TRANSIENT_TRANSFER_ERRORS = (
    ReadTimeoutError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ConnectionClosedError,
    urllib.error.URLError,  # server-backend GET wraps socket.timeout etc.
    TimeoutError,
    ConnectionError,  # builtin: connection reset/aborted
)


def _is_retryable_transfer_error(exc: BaseException) -> bool:
    """Transient (retry) vs. terminal (fail fast). A missing key (404) or auth
    error (403) is terminal; timeouts, dropped connections and 5xx are not."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500  # 404/403 etc. -> don't retry
    if isinstance(exc, _TRANSIENT_TRANSFER_ERRORS):
        return True
    if isinstance(exc, ClientError):  # boto3 5xx / throttling
        code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return code is not None and int(code) >= 500
    return False


def _transfer_with_retry(do_transfer: Callable[[], None], tmp: Path) -> None:
    """Run a download closure with bounded retries on transient errors.

    `do_transfer` must (re)write `tmp` from scratch each call. The partial file
    is cleared between failed attempts. Re-raises on a non-transient error or
    once attempts are exhausted — deliberately: a truly-down bucket must fail,
    not loop.
    """
    for attempt in range(1, _TRANSFER_MAX_ATTEMPTS + 1):
        try:
            do_transfer()
            return
        except Exception as e:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            if attempt == _TRANSFER_MAX_ATTEMPTS or not _is_retryable_transfer_error(e):
                raise
            time.sleep(_TRANSFER_BACKOFF_BASE * (2 ** (attempt - 1)))


# ---------- byte-level progress ----------
#
# `on_bytes` is the fine-grained companion to `download_files`' file-level
# `progress` callback. A session is often a handful of files where one is
# enormous (an axiosaurus_positions capture is easily hundreds of MB), so a
# files-completed counter can sit still for minutes and look hung. `on_bytes`
# reports transfer as it happens.
#
# Signature: on_bytes(key, delta, total, cached)
#
#   delta=None, total=N     declare/reset: `key`'s transferred count is 0 and
#                           its wire size is N bytes (None when unknown). Sent
#                           once before a transfer starts and again on each
#                           retry, so a restarted download can't over-count.
#   delta=D,    total=N     D more bytes of `key` arrived.
#   cached=True             `key` was already local; nothing will transfer.
#                           Renderers should drop it from the byte denominator
#                           (the file-level `progress` callback still counts it).
#
# Byte counts are *wire* bytes: for a .csv.gz key that's the compressed size,
# which is also what the bucket LIST reports, so pre-seeded totals and observed
# deltas are in the same units. Callbacks fire from `download_files`' worker
# threads - renderers must be thread-safe. Exceptions raised by a callback are
# swallowed: a broken progress display must never fail a download.
ByteProgress = Callable[[str, Optional[int], Optional[int], bool], None]

_PROGRESS_CHUNK = 1024 * 1024


def _emit_bytes(
    on_bytes: "ByteProgress | None",
    key: str,
    delta: Optional[int],
    total: Optional[int],
    cached: bool = False,
) -> None:
    if on_bytes is None:
        return
    try:
        on_bytes(key, delta, total, cached)
    except Exception:
        pass


def _copy_with_progress(
    src,
    dst,
    on_bytes: "ByteProgress | None",
    key: str,
    total: Optional[int],
) -> None:
    """copyfileobj that reports each chunk. Plain copyfileobj when unobserved."""
    if on_bytes is None:
        shutil.copyfileobj(src, dst, length=_PROGRESS_CHUNK)
        return
    while True:
        chunk = src.read(_PROGRESS_CHUNK)
        if not chunk:
            return
        dst.write(chunk)
        _emit_bytes(on_bytes, key, len(chunk), total)


class _CountingStream:
    """Read-only passthrough that reports every byte read.

    Lets a .csv.gz key be decompressed *straight off the wire* -
    gzip.GzipFile(fileobj=...) only needs read() - while still counting the
    compressed bytes for progress. The alternative (buffer the whole body, then
    gunzip it) held a 360 MB capture in RAM, and twice that if the buffer was
    copied.
    """

    def __init__(self, src, on_bytes: "ByteProgress | None", key: str, total: Optional[int]):
        self._src = src
        self._on_bytes = on_bytes
        self._key = key
        self._total = total

    def read(self, size: int = -1) -> bytes:
        chunk = self._src.read(size)
        if chunk:
            _emit_bytes(self._on_bytes, self._key, len(chunk), self._total)
        return chunk

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        close = getattr(self._src, "close", None)
        if close is not None:
            close()


def _response_etag(resp) -> Optional[str]:
    """ETag header of an HTTP response, or None."""
    try:
        return resp.headers.get("ETag")
    except AttributeError:
        return None


def _content_length(resp) -> Optional[int]:
    try:
        n = resp.headers.get("Content-Length")
        return int(n) if n is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def object_size(key: str) -> Optional[int]:
    """Wire size of a single key in bytes, or None if it can't be determined.

    Best-effort: the server-mediated backend has no HEAD equivalent, so it
    returns None there and callers fall back to the Content-Length of the
    download itself.
    """
    if _use_server_backend():
        return None
    try:
        return int(_client_singleton().head_object(Bucket=_bucket(), Key=key)["ContentLength"])
    except Exception:
        return None


def list_prefix_sizes(prefix: str) -> dict[str, int]:
    """{key: size_in_bytes} for every object under a prefix (recursive).

    Free next to list_prefix() - list_objects_v2 already returns Size, we just
    normally throw it away. S3 backend only; see session_sizes().
    """
    s3 = _client_singleton()
    out: dict[str, int] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=_bucket(), Prefix=prefix):
        for obj in page.get("Contents", []):
            if is_folder_marker(obj["Key"]):
                continue
            out[obj["Key"]] = int(obj["Size"])
    return out


def session_sizes(device_id: str, date: str) -> dict[str, int]:
    """{key: size} for one session, or {} when sizes aren't available.

    Lets a progress renderer know a session's total download size before the
    first byte arrives. Returns {} on the server-mediated backend (the
    /storage/sessions endpoint returns keys only) and on any listing error -
    an empty map means "unknown", never "empty session".
    """
    if _use_server_backend():
        return {}
    try:
        return list_prefix_sizes(session_prefix(device_id, date))
    except Exception:
        return {}


def ensure_local(
    key: str,
    cache_root: Path | None = None,
    *,
    on_bytes: "ByteProgress | None" = None,
    size_hint: Optional[int] = None,
) -> Path:
    """Download key to cache if not already present. Returns the local Path.

    .csv.gz keys are decompressed on the way in so the local file ends in plain
    .csv — keeps `glob('*.csv')` and the existing date-folder walkers in
    AxioforceNeuralizer working without modification.

    Backend dispatch: with AWS creds set, pulls from Tigris directly via
    boto3. Without creds, asks axio-server for a presigned URL and GETs the
    bytes from Tigris through it (no proxy load on axio-server itself).

    on_bytes:   optional byte-level progress callback - see ByteProgress above.
    size_hint:  this key's wire size, if the caller already knows it (e.g. from
                a bucket LIST). Saves a HEAD round-trip per file; only consulted
                when on_bytes is set.
    """
    _maybe_run_cache_gc()
    _eff_root = cache_root if cache_root is not None else DEFAULT_CACHE_ROOT
    local = cache_path_for_key(key, cache_root)
    refreshing = False
    if local.exists():
        if _cache_hit_is_stale(key, local):
            # The bucket rewrote this key since we cached it. Fall through to a
            # normal download; it lands via tmp + atomic replace, so a failed
            # download raises and leaves the old copy (and its sidecar, which
            # still marks it stale) in place rather than deleting it first.
            refreshing = True
            _gc_logger.info("re-downloading %s: invalidated in the bucket", key)
        else:
            # Cache hit: re-stamp so recency tracks real use (GC's in-flight
            # floor), and keep the verification sidecar in step.
            _cache_gc.mark_used(local, _eff_root)
            _emit_bytes(on_bytes, key, None, None, cached=True)
            return local
    local.parent.mkdir(parents=True, exist_ok=True)
    tmp = local.with_suffix(local.suffix + ".part")

    # Only pay for the HEAD when someone is watching and hasn't told us the size.
    total = size_hint
    if on_bytes is not None and total is None:
        total = object_size(key)

    # ETag of the object actually transferred, set by the transfer closure.
    etag_box: list[Optional[str]] = [None]

    if _use_server_backend():
        # Server-mediated: small POST for the URL, then a direct GET to
        # Tigris (bandwidth-free egress per the bucket pricing).
        info = _server_post_json(
            "/storage/presigned-download", {"key": key, "expires_in": 3600},
        )
        url = info["url"]

        def _server_transfer() -> None:
            # Reset before every attempt: _transfer_with_retry re-runs this
            # closure from scratch, so bytes from a failed attempt must not
            # accumulate.
            _emit_bytes(on_bytes, key, None, total)
            if key.endswith(".csv.gz"):
                with urllib.request.urlopen(url, timeout=300) as resp:
                    etag_box[0] = _response_etag(resp)
                    src = _CountingStream(
                        resp, on_bytes, key, _content_length(resp) or total,
                    )
                    with gzip.GzipFile(fileobj=src) as gz, open(tmp, "wb") as dst:
                        shutil.copyfileobj(gz, dst, length=1024 * 1024)
            else:
                with urllib.request.urlopen(url, timeout=300) as resp, open(tmp, "wb") as dst:
                    etag_box[0] = _response_etag(resp)
                    _copy_with_progress(
                        resp, dst, on_bytes, key, _content_length(resp) or total,
                    )

        _transfer_with_retry(_server_transfer, tmp)
        tmp.replace(local)
        # downloaded => confirmed in bucket; the ETag lets a later hit honor
        # the invalidation manifest.
        _cache_gc.record_verified(local, _eff_root, etag=etag_box[0])
        if refreshing:
            _note_post_refresh_etag(key, etag_box[0])
        return local

    s3 = _client_singleton()

    def _s3_transfer() -> None:
        _emit_bytes(on_bytes, key, None, total)  # reset per attempt (see above)
        if key.endswith(".csv.gz"):
            resp = s3.get_object(Bucket=_bucket(), Key=key)
            etag_box[0] = resp.get("ETag")
            src = _CountingStream(
                resp["Body"], on_bytes, key, resp.get("ContentLength") or total,
            )
            with gzip.GzipFile(fileobj=src) as gz, open(tmp, "wb") as dst:
                shutil.copyfileobj(gz, dst, length=1024 * 1024)
        else:
            # boto3's Callback gets a per-chunk delta, which is exactly what
            # on_bytes wants - including across its multipart worker threads.
            cb = None
            if on_bytes is not None:
                def cb(delta: int) -> None:  # noqa: F811
                    _emit_bytes(on_bytes, key, delta, total)
            # download_file returns no metadata, so HEAD for the ETag. HEAD
            # BEFORE the transfer: if the object is rewritten mid-download we
            # record the older ETag, which can only cause an extra re-download
            # later, never a stale hit. A failed HEAD records "unknown".
            try:
                etag_box[0] = s3.head_object(Bucket=_bucket(), Key=key).get("ETag")
            except Exception:  # noqa: BLE001
                etag_box[0] = None
            s3.download_file(_bucket(), key, str(tmp), Callback=cb)

    _transfer_with_retry(_s3_transfer, tmp)
    tmp.replace(local)
    # downloaded => confirmed in bucket; the ETag lets a later hit honor the
    # invalidation manifest.
    _cache_gc.record_verified(local, _eff_root, etag=etag_box[0])
    if refreshing:
        _note_post_refresh_etag(key, etag_box[0])
    return local


def download_files(
    keys: Sequence[str],
    cache_root: Path | None = None,
    *,
    progress: Callable[[int, int, Optional[str]], None] | None = None,
    on_bytes: "ByteProgress | None" = None,
    sizes: "Mapping[str, int] | None" = None,
    workers: int = 8,
) -> list[Path]:
    """ensure_local() each key in parallel, return local paths in listing order.

    Parallelizes across keys so a session of dozens of small CSVs can saturate
    available bandwidth instead of bottlenecking on per-file TLS/RTT
    overhead. Both backends (boto3 direct and server-mediated presigned URLs)
    benefit — the server backend especially, since it doubles the per-file
    RTT (server hop + Tigris hop). 8 workers matches the migration script
    and gets us close to the network ceiling without overwhelming axio-server
    in the server-mediated case.

    progress: optional callable(idx, total, key). Fires AFTER each completion
              with idx = number of files completed so far and key = the file
              just completed. The final tick is progress(total, total, None).
    on_bytes: optional byte-level callback (see ByteProgress) - fires DURING
              each transfer, so a single huge file still shows movement. Called
              from the worker threads, so it must be thread-safe.
    sizes:    optional {key: wire size} map (e.g. from session_sizes()), passed
              through as each ensure_local's size_hint so an observed batch
              doesn't issue a HEAD per file.
    workers:  thread pool size. Default 8. Set to 1 to disable parallelism
              (deterministic ordering, easier debugging).
    """
    def _fetch(k: str) -> Path:
        return ensure_local(
            k,
            cache_root,
            on_bytes=on_bytes,
            size_hint=(sizes or {}).get(k),
        )

    from concurrent.futures import ThreadPoolExecutor, as_completed

    n = len(keys)
    if n == 0:
        if progress:
            progress(0, 0, None)
        return []

    paths: list[Optional[Path]] = [None] * n

    if workers <= 1 or n == 1:
        for i, k in enumerate(keys):
            paths[i] = _fetch(k)
            if progress:
                progress(i + 1, n, k)
        if progress:
            progress(n, n, None)
        return [p for p in paths]  # type: ignore[misc]

    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_fetch, k): (i, k)
            for i, k in enumerate(keys)
        }
        for fut in as_completed(futures):
            i, k = futures[fut]
            # fut.result() raises any download exception — propagating fails
            # the whole batch fast, which is what we want.
            paths[i] = fut.result()
            completed += 1
            if progress:
                progress(completed, n, k)
    if progress:
        progress(n, n, None)
    return [p for p in paths]  # type: ignore[misc]


def download_session(
    device_id: str,
    date: str,
    cache_root: Path | None = None,
    *,
    progress: Callable[[int, int, Optional[str]], None] | None = None,
    on_bytes: "ByteProgress | None" = None,
    workers: int = 8,
) -> Path:
    """Download every file in a session and return the local session directory.

    The returned dir has the OneDrive-shaped layout (dotted date, decompressed
    CSVs):
        <dir>/calibration_data/train/*.csv
        <dir>/calibration_data/test/*.csv
        <dir>/tests.txt

    progress/on_bytes: file-level and byte-level progress callbacks; see
    download_files(). With on_bytes set, the session's per-file sizes are
    looked up first (one extra LIST, or nothing at all on the server backend)
    and declared up front, so a renderer knows the full download size before
    the first byte lands instead of watching the denominator grow.
    """
    listing = list_session(device_id, date)
    keys = listing.all_keys()
    sizes: dict[str, int] = {}
    if on_bytes is not None:
        sizes = session_sizes(device_id, date)
        for k in keys:
            _emit_bytes(on_bytes, k, None, sizes.get(k))
    download_files(
        keys,
        cache_root=cache_root,
        progress=progress,
        on_bytes=on_bytes,
        sizes=sizes,
        workers=workers,
    )
    root = Path(cache_root) if cache_root else DEFAULT_CACHE_ROOT
    return root / device_type(device_id) / device_id / _dotted_from_iso(date)


# ---------- characterization-grid captures ----------

def grid_prefix(device_id: str, date: Optional[str] = None) -> str:
    """'<type>/<device>/axiosaurus_characterization/' or, with an ISO date,
    that date's directory."""
    p = f"{device_prefix(device_id)}{GRID_DIR}/"
    return f"{p}{date}/" if date else p


def is_grid_capture_key(key: str) -> bool:
    return _GRID_CAPTURE_RE.match(key) is not None


@dataclass
class GridListing:
    """A device's grid captures and the sidecars that describe them.

    A sidecar may list captures of another date (one Axiosaurus run can span
    midnight), so a consumer that parses captures needs the sidecars of
    every grid date of the device, not just the capture's own."""
    captures: list[str]
    sidecars: list[str]

    def all_keys(self) -> list[str]:
        return list(self.captures) + list(self.sidecars)


def _list_keys_recursive(prefix: str) -> list[str]:
    """Every object key under prefix, either backend. The server backend walks
    axio-server's /storage/files one level at a time."""
    if not _use_server_backend():
        return [k for k in list_prefix(prefix, recursive=True) if not is_folder_marker(k)]
    from urllib.parse import quote
    out: list[str] = []
    todo = [prefix]
    while todo:
        p = todo.pop()
        data = _server_get_json(f"/storage/files?prefix={quote(p)}&max_keys=1000")
        if data.get("truncated"):
            raise RuntimeError(f"/storage/files truncated the listing of {p!r} "
                               f"(over 1000 entries); refusing a partial grid listing")
        out += [o["key"] for o in data.get("objects", []) if not is_folder_marker(o["key"])]
        todo += list(data.get("prefixes", []))
    return sorted(out)


def list_grid_dates(device_id: str) -> list[str]:
    """ISO dates that have a grid directory for this device, sorted."""
    keys = _list_keys_recursive(grid_prefix(device_id))
    base = grid_prefix(device_id)
    return sorted({k.removeprefix(base).split("/", 1)[0] for k in keys
                   if ISO_DATE_RE.match(k.removeprefix(base).split("/", 1)[0])})


def list_grid(device_id: str, date: Optional[str] = None) -> GridListing:
    """Grid capture keys (of one date, or all) and every sidecar key of the
    device's grid (g-code + JSON under any '<date>/axiosaurus/')."""
    keys = _list_keys_recursive(grid_prefix(device_id))
    captures = [k for k in keys if is_grid_capture_key(k)
                and (date is None or k.startswith(grid_prefix(device_id, date)))]
    sidecars = [k for k in keys if f"/{GRID_SIDECAR_DIR}/" in k.removeprefix(grid_prefix(device_id))]
    return GridListing(captures=sorted(captures), sidecars=sorted(sidecars))


def download_grid_captures(
    capture_keys: Sequence[str],
    cache_root: Path | None = None,
    *,
    progress: Callable[[int, int, Optional[str]], None] | None = None,
    on_bytes: "ByteProgress | None" = None,
    workers: int = 8,
) -> list[Path]:
    """Download the named grid captures plus every sidecar of their devices,
    and return the captures' local paths (gunzipped '.csv', listing order).

    Raises ValueError on a key that is not a grid capture, and FileNotFoundError
    on one the bucket does not list (a job naming a missing capture must fail,
    not train on less data)."""
    bad = [k for k in capture_keys if not is_grid_capture_key(k)]
    if bad:
        raise ValueError(f"not grid capture keys: {bad}")
    devices = sorted({k.split("/")[1] for k in capture_keys})
    listings = {d: list_grid(d) for d in devices}
    missing = [k for k in capture_keys if k not in listings[k.split("/")[1]].captures]
    if missing:
        raise FileNotFoundError(f"grid captures not in the bucket: {missing}")
    sidecars = sorted({s for g in listings.values() for s in g.sidecars})
    paths = download_files(list(capture_keys) + sidecars, cache_root=cache_root,
                           progress=progress, on_bytes=on_bytes, workers=workers)
    return paths[:len(capture_keys)]


# ---------- upload ----------

def upload_bytes(
    key: str,
    data: bytes,
    *,
    content_type: str = "application/octet-stream",
) -> str:
    """Upload in-memory bytes to a bucket key. Returns the key.

    The public counterpart to ``upload_file`` for callers that generate a body
    rather than read one off disk (e.g. axio-server rendering a shipping PDF).
    Use this instead of reaching for ``_client_singleton()``/``_bucket()``:
    those are private to this module and are NOT re-exported by
    ``axio_common.storage`` (``from .storage_core import *`` skips
    underscore-prefixed names), so touching them from another package raises
    AttributeError at runtime.

    Backend selection mirrors ``upload_file``:
      - S3 backend (creds present): boto3 puts the body directly to Tigris.
      - Server-mediated backend (no creds): asks axio-server for a presigned
        PUT URL, then PUTs the body over that URL.

    No cache bookkeeping — there is no local file to mark verified.
    """
    _assert_canonical_key(key)

    if _use_server_backend():
        info = _server_post_json(
            "/storage/presigned-upload-by-key",
            {"key": key, "expires_in": 3600},
        )
        put_req = urllib.request.Request(
            info["url"],
            data=data,
            method="PUT",
            headers={"Content-Type": content_type},
        )
        # One-shot PUT, no retry inside; failure bubbles to the caller.
        with urllib.request.urlopen(put_req, timeout=300) as resp:
            resp.read()  # drain
        return key

    _client_singleton().put_object(
        Bucket=_bucket(), Key=key, Body=data, ContentType=content_type,
    )
    return key


def upload_file(
    local_path: str | Path,
    key: str,
    *,
    compress_csv: bool = True,
) -> str:
    """Upload a local file to a specific bucket key.

    If compress_csv=True and the local file ends with .csv, the upload is
    gzip-compressed in flight and the key gets a .gz extension appended (if
    not already present).

    Backend selection mirrors the read path:
      - S3 backend (creds present): boto3 puts the body directly to Tigris.
      - Server-mediated backend (no creds): asks axio-server for a presigned
        PUT URL, then PUTs the body straight to Tigris over that URL. The
        bucket secret never leaves axio-server.
    """
    _assert_canonical_key(key)
    _maybe_run_cache_gc()
    local = Path(local_path)

    # Materialize the body once so both backends use the same bytes.
    if compress_csv and local.suffix.lower() == ".csv":
        if not key.endswith(".gz"):
            key = key + ".gz"
        buf = io.BytesIO()
        with open(local, "rb") as src, gzip.GzipFile(
            fileobj=buf, mode="wb", compresslevel=6, mtime=0
        ) as gz:
            shutil.copyfileobj(src, gz, length=1024 * 1024)
        body = buf.getvalue()
        content_type = "application/gzip"
    else:
        with open(local, "rb") as src:
            body = src.read()
        content_type = "application/octet-stream"

    if _use_server_backend():
        info = _server_post_json(
            "/storage/presigned-upload-by-key",
            {"key": key, "expires_in": 3600},
        )
        put_req = urllib.request.Request(
            info["url"],
            data=body,
            method="PUT",
            headers={"Content-Type": content_type},
        )
        # Long timeout: a multi-MB CSV upload over a slow link can take a
        # while, but this is a one-shot PUT with no retry inside; failure
        # bubbles up to the caller.
        with urllib.request.urlopen(put_req, timeout=300) as resp:
            resp.read()  # drain
        _cache_gc.record_verified(local, DEFAULT_CACHE_ROOT)  # uploaded => confirmed in bucket
        return key

    s3 = _client_singleton()
    if compress_csv and local.suffix.lower() == ".csv":
        s3.upload_fileobj(
            io.BytesIO(body), _bucket(), key,
            ExtraArgs={"ContentType": content_type},
        )
    else:
        s3.upload_file(str(local), _bucket(), key)
    _cache_gc.record_verified(local, DEFAULT_CACHE_ROOT)  # uploaded => confirmed in bucket
    return key


def upload_session_files(
    device_id: str,
    date: str,
    *,
    train_files: Iterable[str | Path] = (),
    test_files: Iterable[str | Path] = (),
) -> dict[str, list[str]]:
    """Convenience helper: upload local CSVs into a session's train/test prefixes.

    Returns the keys written: {"train": [...], "test": [...]}.
    Idempotent — re-uploading overwrites.
    """
    out: dict[str, list[str]] = {"train": [], "test": []}
    for p in train_files:
        p = Path(p)
        out["train"].append(upload_file(p, make_key(device_id, date, "train", p.name)))
    for p in test_files:
        p = Path(p)
        out["test"].append(upload_file(p, make_key(device_id, date, "test", p.name)))
    return out


# ---------- presigned URLs ----------

def presigned_put_url(key: str, expires_in: int = 3600) -> str:
    _assert_canonical_key(key)
    return _client_singleton().generate_presigned_url(
        "put_object", Params={"Bucket": _bucket(), "Key": key}, ExpiresIn=expires_in
    )


def presigned_get_url(key: str, expires_in: int = 3600) -> str:
    return _client_singleton().generate_presigned_url(
        "get_object", Params={"Bucket": _bucket(), "Key": key}, ExpiresIn=expires_in
    )
