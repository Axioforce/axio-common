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

Configuration via env vars (boto3 picks them up automatically):
    AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY
    AWS_ENDPOINT_URL_S3       (default: https://fly.storage.tigris.dev)
    AWS_REGION                (default: auto)
    BUCKET_NAME               (default: axioforce-calibration)
    AXIO_CALIBRATION_CACHE    (default: ~/.axio-cache)

Cache: ensure_local() lazily downloads keys to AXIO_CALIBRATION_CACHE, mirroring the
bucket layout but inserting "calibration_data/" between the date and {train,test}
so that downstream code which walks date directories sees the OneDrive-shaped
structure it expects.

Eviction: not yet implemented. The cache grows monotonically; clean it manually when
needed (rm -rf $AXIO_CALIBRATION_CACHE). LRU eviction by total bytes is on the roadmap.

NOTE: requires boto3. Add it to axio-common's pyproject.toml dependencies before shipping.
"""
from __future__ import annotations

import gzip
import io
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

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

_client = None


def _client_singleton():
    """Lazy-init boto3 client. Reuses connection pool across calls."""
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3", DEFAULT_ENDPOINT),
            region_name=os.environ.get("AWS_REGION", DEFAULT_REGION),
            config=Config(signature_version="s3v4", max_pool_connections=32),
        )
    return _client


def _bucket() -> str:
    return os.environ.get("BUCKET_NAME", DEFAULT_BUCKET)


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
    return list_top_dirs("")


def list_devices(device_type_id: str) -> list[str]:
    """Device ids under a type, e.g. ['10-00000002', '10-00000003', ...]."""
    return list_top_dirs(f"{device_type_id}/")


def list_dates(device_id: str) -> list[str]:
    """Date strings (and 'models') under a device prefix.

    Returns ISO dates first (sorted) then 'models' if present.
    """
    children = list_top_dirs(device_prefix(device_id))
    dates = sorted(c for c in children if c != "models")
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


def list_session(device_id: str, date: str) -> SessionListing:
    """All keys under one (device, date) session, split by kind."""
    keys = list_prefix(session_prefix(device_id, date), recursive=True)
    train, test, other = [], [], []
    tests_txt: Optional[str] = None
    base = session_prefix(device_id, date)
    for k in keys:
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


def ensure_local(key: str, cache_root: Path | None = None) -> Path:
    """Download key to cache if not already present. Returns the local Path.

    .csv.gz keys are decompressed on the way in so the local file ends in plain
    .csv — keeps `glob('*.csv')` and the existing date-folder walkers in
    AxioforceNeuralizer working without modification."""
    local = cache_path_for_key(key, cache_root)
    if local.exists():
        return local
    local.parent.mkdir(parents=True, exist_ok=True)
    s3 = _client_singleton()
    tmp = local.with_suffix(local.suffix + ".part")
    if key.endswith(".csv.gz"):
        body = s3.get_object(Bucket=_bucket(), Key=key)["Body"].read()
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as gz, open(tmp, "wb") as dst:
            shutil.copyfileobj(gz, dst, length=1024 * 1024)
    else:
        s3.download_file(_bucket(), key, str(tmp))
    tmp.replace(local)
    return local


def download_files(
    keys: Sequence[str],
    cache_root: Path | None = None,
    *,
    progress: Callable[[int, int, Optional[str]], None] | None = None,
) -> list[Path]:
    """ensure_local() each key, return their local paths in the same order.

    progress: optional callable(idx, total, key) for per-file progress reporting.
    """
    paths: list[Path] = []
    total = len(keys)
    for i, k in enumerate(keys):
        if progress:
            progress(i, total, k)
        paths.append(ensure_local(k, cache_root))
    if progress:
        progress(total, total, None)
    return paths


def download_session(
    device_id: str,
    date: str,
    cache_root: Path | None = None,
    *,
    progress: Callable[[int, int, Optional[str]], None] | None = None,
) -> Path:
    """Download every file in a session and return the local session directory.

    The returned dir has the OneDrive-shaped layout (dotted date, decompressed
    CSVs):
        <dir>/calibration_data/train/*.csv
        <dir>/calibration_data/test/*.csv
        <dir>/tests.txt
    """
    listing = list_session(device_id, date)
    download_files(listing.all_keys(), cache_root=cache_root, progress=progress)
    root = Path(cache_root) if cache_root else DEFAULT_CACHE_ROOT
    return root / device_type(device_id) / device_id / _dotted_from_iso(date)


# ---------- upload ----------

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
    """
    local = Path(local_path)
    s3 = _client_singleton()

    if compress_csv and local.suffix.lower() == ".csv":
        if not key.endswith(".gz"):
            key = key + ".gz"
        buf = io.BytesIO()
        with open(local, "rb") as src, gzip.GzipFile(
            fileobj=buf, mode="wb", compresslevel=6, mtime=0
        ) as gz:
            shutil.copyfileobj(src, gz, length=1024 * 1024)
        buf.seek(0)
        s3.upload_fileobj(buf, _bucket(), key, ExtraArgs={"ContentType": "application/gzip"})
    else:
        s3.upload_file(str(local), _bucket(), key)
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
    return _client_singleton().generate_presigned_url(
        "put_object", Params={"Bucket": _bucket(), "Key": key}, ExpiresIn=expires_in
    )


def presigned_get_url(key: str, expires_in: int = 3600) -> str:
    return _client_singleton().generate_presigned_url(
        "get_object", Params={"Bucket": _bucket(), "Key": key}, ExpiresIn=expires_in
    )
