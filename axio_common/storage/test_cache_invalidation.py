"""Tests for the bucket-driven cache invalidation manifest (storage_core) and
the ETag-recording verification sidecars (cache_gc). No network: both backends
are replaced with in-memory fakes."""
from __future__ import annotations

import gzip
import io
import json
import os
import time
import urllib.error

import pytest
from botocore.exceptions import ClientError

from axio_common.storage import cache_gc
from axio_common.storage import invalidate as inv
from axio_common.storage import storage_core as sc

GZ_KEY = "18/18-0000abcd/2026-10-01/train/18-0000abcd-TR-X_10.01.2026.csv.gz"
TXT_KEY = "18/18-0000abcd/2026-10-01/tests.txt"
OTHER_KEY = "10/10-00000002/2024-12-20/train/10-00000002-TR-BER_12.20.2024.csv.gz"


def _gz(text: str) -> bytes:
    return gzip.compress(text.encode())


def _not_found(op: str) -> ClientError:
    return ClientError({"Error": {"Code": "NoSuchKey", "Message": "nope"}}, op)


class FakeS3:
    """Just enough of a boto3 S3 client. objects: key -> (bytes, etag)."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_manifest = False

    def put(self, key: str, body: bytes, etag: str) -> None:
        self.objects[key] = (body, f'"{etag}"')

    def _get(self, op, key):
        self.calls.append((op, key))
        if key == sc.INVALIDATION_MANIFEST_KEY and self.fail_manifest:
            raise RuntimeError("bucket down")
        if key not in self.objects:
            raise _not_found(op)
        return self.objects[key]

    def get_object(self, Bucket, Key):
        body, etag = self._get("get_object", Key)
        return {"Body": io.BytesIO(body), "ETag": etag, "ContentLength": len(body)}

    def head_object(self, Bucket, Key):
        body, etag = self._get("head_object", Key)
        return {"ETag": etag, "ContentLength": len(body)}

    def download_file(self, Bucket, Key, Filename, Callback=None):
        body, _ = self._get("download_file", Key)
        with open(Filename, "wb") as f:
            f.write(body)

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.calls.append(("put_object", Key))
        self.objects[Key] = (Body, '"new"')

    def manifest_fetches(self) -> int:
        return sum(1 for op, k in self.calls if k == sc.INVALIDATION_MANIFEST_KEY and op == "get_object")


def _manifest(entries: dict[str, str]) -> bytes:
    return json.dumps({
        "version": 1,
        "entries": {k: {"etag": e, "reason": "test", "at": "2026-10-26T00:00:00+00:00"}
                    for k, e in entries.items()},
    }).encode()


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setenv("AXIO_CALIBRATION_CACHE_GC", "0")
    monkeypatch.setattr(sc, "_use_server_backend", lambda: False)
    monkeypatch.setattr(sc, "_client_singleton", lambda: fake)
    sc._reset_invalidation_manifest()
    yield fake
    sc._reset_invalidation_manifest()


def _read(path) -> str:
    return open(path, encoding="utf-8").read()


# ---------- sidecar primitives ----------

def test_record_verified_writes_etag_and_keeps_mtime_semantics(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("x")
    cache_gc.record_verified(f, tmp_path, etag='"abc"')
    assert cache_gc.read_verified_etag(f) == "abc"
    assert cache_gc.is_verified(f)
    # a re-capture (newer file mtime) is un-verified even though the ETag is there
    later = time.time() + 100
    os.utime(f, (later, later))
    assert not cache_gc.is_verified(f)
    # mark_used re-stamps both and keeps the content
    cache_gc.mark_used(f, tmp_path, now=later + 10)
    assert cache_gc.is_verified(f)
    assert cache_gc.read_verified_etag(f) == "abc"
    # an upload (no ETag) clears a stale ETag
    cache_gc.record_verified(f, tmp_path, now=later + 20)
    assert cache_gc.read_verified_etag(f) is None
    assert cache_gc.is_verified(f)


def test_legacy_empty_sidecar_reads_as_unknown(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("x")
    cache_gc._sidecar_for(f).touch()
    assert cache_gc.read_verified_etag(f) is None
    assert cache_gc.is_verified(f)


# ---------- ensure_local: ETag recorded on download (S3 backend) ----------

def test_s3_gz_download_records_etag(s3, tmp_path):
    s3.put(GZ_KEY, _gz("a,b\n1,2\n"), "e1")
    p = sc.ensure_local(GZ_KEY, cache_root=tmp_path)
    assert _read(p) == "a,b\n1,2\n"
    assert cache_gc.read_verified_etag(p) == "e1"
    assert cache_gc.is_verified(p)


def test_s3_plain_download_records_etag_via_head(s3, tmp_path):
    s3.put(TXT_KEY, b"tests", "t1")
    p = sc.ensure_local(TXT_KEY, cache_root=tmp_path)
    assert _read(p) == "tests"
    assert cache_gc.read_verified_etag(p) == "t1"
    # HEAD came before the transfer (see ensure_local)
    ops = [op for op, k in s3.calls if k == TXT_KEY]
    assert ops == ["head_object", "download_file"]


# ---------- ensure_local: ETag recorded on download (server backend) ----------

class _FakeResp(io.BytesIO):
    def __init__(self, body: bytes, etag: str | None):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body))}
        if etag is not None:
            self.headers["ETag"] = etag

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


@pytest.fixture
def server(monkeypatch):
    """Server-mediated backend: presigned URL = 'url:<key>'."""
    objects: dict[str, tuple[bytes, str]] = {}
    calls: list[str] = []

    def post(path, body, timeout=30):
        assert path == "/storage/presigned-download"
        return {"url": f"url:{body['key']}", "key": body["key"]}

    def urlopen(url, timeout=None):
        key = url.removeprefix("url:")
        calls.append(key)
        if key not in objects:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        body, etag = objects[key]
        return _FakeResp(body, etag)

    monkeypatch.setenv("AXIO_CALIBRATION_CACHE_GC", "0")
    monkeypatch.setattr(sc, "_use_server_backend", lambda: True)
    monkeypatch.setattr(sc, "_server_post_json", post)
    monkeypatch.setattr(sc.urllib.request, "urlopen", urlopen)
    sc._reset_invalidation_manifest()
    yield objects, calls
    sc._reset_invalidation_manifest()


def test_server_gz_download_records_etag(server, tmp_path):
    objects, _ = server
    objects[GZ_KEY] = (_gz("v1"), '"s1"')
    p = sc.ensure_local(GZ_KEY, cache_root=tmp_path)
    assert _read(p) == "v1"
    assert cache_gc.read_verified_etag(p) == "s1"


def test_server_plain_download_records_etag(server, tmp_path):
    objects, _ = server
    objects[TXT_KEY] = (b"tests", '"s2"')
    p = sc.ensure_local(TXT_KEY, cache_root=tmp_path)
    assert cache_gc.read_verified_etag(p) == "s2"


def test_server_backend_invalidates_and_404_manifest_is_empty(server, tmp_path):
    objects, calls = server
    objects[GZ_KEY] = (_gz("old"), '"o"')
    p = sc.ensure_local(GZ_KEY, cache_root=tmp_path)
    # no manifest yet (404) -> plain hit
    assert sc.ensure_local(GZ_KEY, cache_root=tmp_path) == p
    assert _read(p) == "old"
    # rewrite + manifest; the TTL'd empty manifest must be dropped to see it
    objects[GZ_KEY] = (_gz("new"), '"n"')
    objects[sc.INVALIDATION_MANIFEST_KEY] = (_manifest({GZ_KEY: "n"}), '"m"')
    sc._reset_invalidation_manifest()
    assert _read(sc.ensure_local(GZ_KEY, cache_root=tmp_path)) == "new"
    assert cache_gc.read_verified_etag(p) == "n"


# ---------- cache-hit decisions ----------

def _seed(s3, tmp_path, key=GZ_KEY, body="old", etag="old", legacy=False):
    """Cache `key` via a real download of version `etag`, then rewrite the
    bucket object to 'new'. legacy=True blanks the sidecar (pre-ETag cache)."""
    s3.put(key, _gz(body), etag)
    p = sc.ensure_local(key, cache_root=tmp_path)
    if legacy:
        sc_path = cache_gc._sidecar_for(p)
        st = sc_path.stat()
        sc_path.write_text("")
        os.utime(sc_path, (st.st_atime, st.st_mtime))
    s3.put(key, _gz("new"), "new")
    return p


def test_legacy_sidecar_and_key_in_manifest_redownloads(s3, tmp_path):
    p = _seed(s3, tmp_path, legacy=True)
    s3.put(sc.INVALIDATION_MANIFEST_KEY, _manifest({GZ_KEY: "new"}), "m")
    assert _read(sc.ensure_local(GZ_KEY, cache_root=tmp_path)) == "new"
    assert cache_gc.read_verified_etag(p) == "new"
    # now in step: the next call is a hit with no further object traffic
    n = len(s3.calls)
    assert _read(sc.ensure_local(GZ_KEY, cache_root=tmp_path)) == "new"
    assert len(s3.calls) == n


def test_old_etag_and_key_in_manifest_redownloads(s3, tmp_path):
    p = _seed(s3, tmp_path)
    s3.put(sc.INVALIDATION_MANIFEST_KEY, _manifest({GZ_KEY: '"new"'}), "m")
    assert _read(sc.ensure_local(GZ_KEY, cache_root=tmp_path)) == "new"
    assert cache_gc.read_verified_etag(p) == "new"


def test_matching_etag_is_a_hit(s3, tmp_path):
    s3.put(GZ_KEY, _gz("cur"), "cur")
    p = sc.ensure_local(GZ_KEY, cache_root=tmp_path)
    s3.put(sc.INVALIDATION_MANIFEST_KEY, _manifest({GZ_KEY: "cur"}), "m")
    n_get = sum(1 for op, k in s3.calls if k == GZ_KEY)
    assert sc.ensure_local(GZ_KEY, cache_root=tmp_path) == p
    assert sum(1 for op, k in s3.calls if k == GZ_KEY) == n_get  # no re-download


def test_key_not_in_manifest_is_free(s3, tmp_path):
    p = _seed(s3, tmp_path)  # bucket now differs, but the manifest doesn't say so
    s3.put(sc.INVALIDATION_MANIFEST_KEY, _manifest({OTHER_KEY: "zzz"}), "m")
    n = len(s3.calls)
    for _ in range(5):
        assert _read(sc.ensure_local(GZ_KEY, cache_root=tmp_path)) == "old"
    new_calls = s3.calls[n:]
    # exactly one manifest load for five hits, nothing else
    assert new_calls == [("get_object", sc.INVALIDATION_MANIFEST_KEY)]
    assert cache_gc.read_verified_etag(p) == "old"


def test_manifest_reloads_after_ttl(s3, tmp_path, monkeypatch):
    _seed(s3, tmp_path)
    sc.ensure_local(GZ_KEY, cache_root=tmp_path)
    assert s3.manifest_fetches() == 1
    sc.ensure_local(GZ_KEY, cache_root=tmp_path)
    assert s3.manifest_fetches() == 1
    real = time.monotonic
    monkeypatch.setattr(sc.time, "monotonic", lambda: real() + sc.INVALIDATION_TTL_S + 1)
    sc.ensure_local(GZ_KEY, cache_root=tmp_path)
    assert s3.manifest_fetches() == 2


def test_manifest_fetch_failure_fails_open(s3, tmp_path, caplog):
    p = _seed(s3, tmp_path)
    s3.put(sc.INVALIDATION_MANIFEST_KEY, _manifest({GZ_KEY: "new"}), "m")
    s3.fail_manifest = True
    with caplog.at_level("WARNING", logger=sc.__name__):
        for _ in range(3):
            assert _read(sc.ensure_local(GZ_KEY, cache_root=tmp_path)) == "old"
    assert s3.manifest_fetches() == 1  # failure is cached for the TTL too
    assert sum("could not be loaded" in r.message for r in caplog.records) == 1
    assert cache_gc.read_verified_etag(p) == "old"


def test_malformed_manifest_fails_open(s3, tmp_path):
    _seed(s3, tmp_path)
    s3.put(sc.INVALIDATION_MANIFEST_KEY, b"{not json", "m")
    assert _read(sc.ensure_local(GZ_KEY, cache_root=tmp_path)) == "old"


def test_unverified_local_file_is_never_replaced(s3, tmp_path):
    """A file rewritten locally after its download (an un-uploaded re-capture)
    is un-verified; the manifest must not destroy it."""
    p = _seed(s3, tmp_path)
    later = time.time() + 100
    p.write_text("local re-capture")
    os.utime(p, (later, later))
    s3.put(sc.INVALIDATION_MANIFEST_KEY, _manifest({GZ_KEY: "new"}), "m")
    assert _read(sc.ensure_local(GZ_KEY, cache_root=tmp_path)) == "local re-capture"


def test_failed_refresh_raises_and_keeps_old_copy(s3, tmp_path):
    p = _seed(s3, tmp_path)
    s3.put(sc.INVALIDATION_MANIFEST_KEY, _manifest({GZ_KEY: "new"}), "m")
    del s3.objects[GZ_KEY]
    with pytest.raises(ClientError):
        sc.ensure_local(GZ_KEY, cache_root=tmp_path)
    assert _read(p) == "old"


def test_download_files_and_session_route_through_check(s3, tmp_path, monkeypatch):
    p = _seed(s3, tmp_path)
    s3.put(sc.INVALIDATION_MANIFEST_KEY, _manifest({GZ_KEY: "new"}), "m")
    [q] = sc.download_files([GZ_KEY], cache_root=tmp_path, workers=1)
    assert q == p and _read(p) == "new"
    # download_session -> download_files -> ensure_local
    s3.put(GZ_KEY, _gz("newer"), "newer")
    s3.put(sc.INVALIDATION_MANIFEST_KEY, _manifest({GZ_KEY: "newer"}), "m2")
    sc._reset_invalidation_manifest()
    monkeypatch.setattr(sc, "list_session", lambda d, dt: sc.SessionListing(
        train=[GZ_KEY], test=[], tests_txt=None, other=[]))
    sc.download_session("18-0000abcd", "2026-10-01", cache_root=tmp_path, workers=1)
    assert _read(p) == "newer"


# ---------- operator helper ----------

def test_invalidate_cli_merges_and_dry_run_writes_nothing(s3, capsys):
    s3.put(GZ_KEY, b"x", "g1")
    s3.put(TXT_KEY, b"y", "t1")
    s3.put(sc.INVALIDATION_MANIFEST_KEY, _manifest({OTHER_KEY: "keep", GZ_KEY: "stale"}), "m")

    assert inv.main([GZ_KEY, TXT_KEY, "--reason", "r", "--dry-run"]) == 0
    assert ("put_object", sc.INVALIDATION_MANIFEST_KEY) not in s3.calls

    assert inv.main([GZ_KEY, TXT_KEY, "--reason", "frame fix"]) == 0
    doc = json.loads(s3.objects[sc.INVALIDATION_MANIFEST_KEY][0])
    assert doc["version"] == 1
    assert doc["entries"][OTHER_KEY]["etag"] == "keep"        # kept
    assert doc["entries"][GZ_KEY]["etag"] == "g1"             # replaced
    assert doc["entries"][TXT_KEY]["etag"] == "t1"            # added
    assert doc["entries"][TXT_KEY]["reason"] == "frame fix"
    assert sc.parse_invalidation_manifest(s3.objects[sc.INVALIDATION_MANIFEST_KEY][0]) == {
        OTHER_KEY: "keep", GZ_KEY: "g1", TXT_KEY: "t1"}


def test_invalidate_cli_missing_key_aborts(s3):
    s3.put(GZ_KEY, b"x", "g1")
    assert inv.main([GZ_KEY, "18/nope.csv.gz", "--reason", "r"]) == 1
    assert sc.INVALIDATION_MANIFEST_KEY not in s3.objects


def test_invalidate_cli_refuses_server_backend(monkeypatch):
    monkeypatch.setattr(sc, "_use_server_backend", lambda: True)
    assert inv.main([GZ_KEY, "--reason", "r"]) == 2
