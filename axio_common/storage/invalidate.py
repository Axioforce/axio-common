"""
Mark bucket keys as rewritten, so every machine's cache re-downloads them.

    python -m axio_common.storage.invalidate <key> [<key> ...] --reason "..." [--dry-run]

Run this AFTER overwriting the objects. For each key it HEADs the bucket for the
object's CURRENT ETag and merges `{key: {"etag", "reason", "at"}}` into the
manifest at `_config/cache_invalidations.json` (read-modify-write: existing
entries are kept, an entry for the same key is replaced). Any cache whose copy
of a listed key was downloaded with a different ETag (or before ETags were
recorded) re-downloads it on its next cache hit. See storage_core's
"bucket-driven cache invalidation" section and storage/README.md.

If an object is rewritten AGAIN later, re-run this for it: a stale manifest
ETag makes every cache hit on that key re-download.

Needs direct bucket credentials (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY);
the server-mediated backend cannot write `_config/`. The read-modify-write is
not atomic: don't run two of these at once.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from typing import Optional, Sequence

from botocore.exceptions import ClientError

from . import cache_gc as _cache_gc
from . import storage_core as _sc


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _is_not_found(e: ClientError) -> bool:
    return e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound")


def read_manifest(s3, bucket: str) -> dict:
    """The current manifest document, or an empty version-1 one if absent."""
    try:
        resp = s3.get_object(Bucket=bucket, Key=_sc.INVALIDATION_MANIFEST_KEY)
    except ClientError as e:
        if _is_not_found(e):
            return {"version": 1, "entries": {}}
        raise
    doc = json.loads(resp["Body"].read())
    _sc.parse_invalidation_manifest(json.dumps(doc))  # validates the version
    doc.setdefault("entries", {})
    return doc


def current_etag(s3, bucket: str, key: str) -> str:
    """Normalized ETag of the object at `key`. Raises if it doesn't exist."""
    etag = _cache_gc._normalize_etag(s3.head_object(Bucket=bucket, Key=key).get("ETag"))
    if not etag:
        raise RuntimeError(f"{key}: HEAD returned no ETag")
    return etag


def merge_entries(doc: dict, new: dict) -> dict:
    """Return `doc` with `new` ({key: entry}) merged in; same key -> replaced."""
    out = {"version": 1, "entries": dict(doc.get("entries") or {})}
    out["entries"].update(new)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m axio_common.storage.invalidate",
        description="Record bucket keys as rewritten so caches re-download them.",
    )
    ap.add_argument("keys", nargs="+", help="bucket keys (canonical, e.g. 18/18-xxxxxxxx/2026-10-01/train/x.csv.gz)")
    ap.add_argument("--reason", required=True, help="why the objects were rewritten")
    ap.add_argument("--dry-run", action="store_true", help="HEAD the keys and print the result; write nothing")
    args = ap.parse_args(argv)

    if _sc._use_server_backend():
        print("error: needs direct bucket credentials (AWS_ACCESS_KEY_ID / "
              "AWS_SECRET_ACCESS_KEY); the server-mediated backend cannot write "
              f"{_sc.INVALIDATION_MANIFEST_KEY}.", file=sys.stderr)
        return 2

    s3 = _sc._client_singleton()
    bucket = _sc._bucket()
    at = _now_iso()

    # HEAD every key first; any failure aborts before the manifest is touched.
    new: dict[str, dict] = {}
    failed = False
    for key in dict.fromkeys(args.keys):  # de-dupe, keep order
        if key == _sc.INVALIDATION_MANIFEST_KEY:
            print("error: refusing to list the manifest itself", file=sys.stderr)
            failed = True
            continue
        try:
            etag = current_etag(s3, bucket, key)
        except ClientError as e:
            what = "not found" if _is_not_found(e) else str(e)
            print(f"error: {key}: {what}", file=sys.stderr)
            failed = True
            continue
        new[key] = {"etag": etag, "reason": args.reason, "at": at}
        print(f"{key}  etag={etag}")
    if failed:
        print("aborted: manifest not changed", file=sys.stderr)
        return 1

    doc = read_manifest(s3, bucket)
    before = doc.get("entries") or {}
    replaced = sorted(k for k in new if k in before)
    merged = merge_entries(doc, new)
    print(f"manifest {bucket}/{_sc.INVALIDATION_MANIFEST_KEY}: "
          f"{len(before)} existing entr{'y' if len(before) == 1 else 'ies'}, "
          f"{len(new) - len(replaced)} added, {len(replaced)} replaced, "
          f"{len(merged['entries'])} after")

    if args.dry_run:
        print("dry run: nothing written")
        return 0

    s3.put_object(
        Bucket=bucket,
        Key=_sc.INVALIDATION_MANIFEST_KEY,
        Body=json.dumps(merged, indent=2, sort_keys=True).encode("utf-8"),
        ContentType="application/json",
    )
    print("written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
