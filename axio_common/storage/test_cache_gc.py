"""Unit tests for cache_gc — runnable via pytest or as a plain script
(`python test_cache_gc.py`). Stdlib only; no network, no bucket.

Mirror these into the DynamoPy copy too if the policy changes."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

# Load the stdlib-only module directly by path so this test needs none of the
# package's heavier deps (boto3/pydantic pulled in by axio_common.storage.__init__).
_spec = importlib.util.spec_from_file_location(
    "cache_gc", Path(__file__).resolve().parent / "cache_gc.py"
)
cache_gc = importlib.util.module_from_spec(_spec)
sys.modules["cache_gc"] = cache_gc  # let dataclasses resolve annotations
_spec.loader.exec_module(cache_gc)

GB = 1024 ** 3
DAY = 86400.0
NOW = 1_700_000_000.0  # fixed clock so tests are deterministic


def _mkfile(root: Path, rel: str, *, size: int, age_days: float, verified: bool) -> Path:
    """Create a cache file aged `age_days`. If verified, also drop a sidecar
    stamped at the file's mtime (i.e. 'confirmed in bucket at write time')."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    mt = NOW - age_days * DAY
    os.utime(p, (mt, mt))
    if verified:
        # sidecar stamped exactly at file mtime — the boundary case (>= passes)
        cache_gc.record_verified(p, root, now=mt)
    return p


# ---------- unit detection ----------

def test_unit_detection():
    assert cache_gc._unit_parts(
        ("10", "dev", "12.20.2024", "calibration_data", "train", "a.csv")
    ) == ("10", "dev", "12.20.2024")
    assert cache_gc._unit_parts(
        ("10", "dev", "models", "compound", "best_model", "m.tflite")
    ) == ("10", "dev", "models", "compound")
    assert cache_gc._unit_parts(("README.txt",)) is None


# ---------- sidecar verification primitives ----------

def test_sidecar_roundtrip_and_recapture():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        f = _mkfile(root, "10/dev/01.01.2024/calibration_data/train/x.csv", size=100, age_days=60, verified=True)
        assert cache_gc._is_verified(f, f.stat().st_mtime)
        # simulate a re-capture: rewrite the file with a NEWER mtime, no new sidecar
        newer = NOW - 1 * DAY
        f.write_bytes(b"y" * 200)
        os.utime(f, (newer, newer))
        assert not cache_gc._is_verified(f, f.stat().st_mtime)  # stale sidecar -> un-verified


def test_sidecar_outside_root_is_noop():
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        root = Path(d1)
        outside = Path(d2) / "x.csv"
        outside.write_bytes(b"x")
        cache_gc.record_verified(outside, root, now=NOW)  # guarded -> no sidecar
        assert not cache_gc._sidecar_for(outside).exists()


# ---------- TTL stage ----------

def test_ttl_evicts_old_verified_only():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _mkfile(root, "10/dev/01.01.2024/calibration_data/train/old.csv", size=100, age_days=60, verified=True)
        _mkfile(root, "10/dev/06.01.2024/calibration_data/train/new.csv", size=100, age_days=2, verified=True)
        rep = cache_gc.run_gc(root, max_gb=1000, max_age_days=28, recency_floor_hours=24, now=NOW)
        assert rep.evicted_units == ["10/dev/01.01.2024"]
        assert not (root / "10/dev/01.01.2024").exists()
        assert (root / "10/dev/06.01.2024").exists()


def test_unverified_never_evicted_even_when_old():
    """The C1 invariant: an un-uploaded (un-verified) file is never deleted,
    even when it is older than the TTL and the cap is zero."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _mkfile(root, "10/dev/01.01.2024/calibration_data/train/old.csv", size=100, age_days=60, verified=False)
        rep = cache_gc.run_gc(root, max_gb=0, max_age_days=1, recency_floor_hours=0, now=NOW)
        assert rep.evicted_units == []
        assert rep.pending_files == 1
        assert (root / "10/dev/01.01.2024").exists()


def test_partial_unit_unverified_protects_whole_unit():
    """One un-verified file (e.g. an in-progress training output) keeps the
    entire unit un-evictable."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _mkfile(root, "10/dev/01.01.2024/calibration_data/train/a.csv", size=100, age_days=60, verified=True)
        _mkfile(root, "10/dev/01.01.2024/calibration_data/train/b.csv", size=100, age_days=60, verified=False)
        rep = cache_gc.run_gc(root, max_gb=0, max_age_days=1, recency_floor_hours=0, now=NOW)
        assert rep.evicted_units == []
        assert (root / "10/dev/01.01.2024").exists()


def test_recency_floor_protects_fresh():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _mkfile(root, "10/dev/01.01.2024/calibration_data/train/x.csv", size=100, age_days=0.04, verified=True)
        rep = cache_gc.run_gc(root, max_gb=0, max_age_days=0, recency_floor_hours=24, now=NOW)
        assert rep.evicted_units == []
        assert (root / "10/dev/01.01.2024").exists()


def test_mark_used_keeps_verified_and_refreshes_recency():
    """mark_used (cache-hit) re-stamps file+sidecar to now: the unit stays
    verified AND becomes fresh, so the recency floor now protects it."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        f = _mkfile(root, "10/dev/01.01.2024/calibration_data/train/x.csv", size=100, age_days=60, verified=True)
        cache_gc.mark_used(f, root, now=NOW)
        assert cache_gc._is_verified(f, f.stat().st_mtime)  # still verified
        rep = cache_gc.run_gc(root, max_gb=0, max_age_days=0, recency_floor_hours=24, now=NOW)
        assert rep.evicted_units == []  # fresh again -> protected


# ---------- cap stage ----------

def test_cap_evicts_oldest_first_until_under():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _mkfile(root, "10/dev/03.01.2024/calibration_data/train/a.csv", size=2000, age_days=10, verified=True)
        _mkfile(root, "10/dev/03.02.2024/calibration_data/train/b.csv", size=2000, age_days=8, verified=True)
        _mkfile(root, "10/dev/03.03.2024/calibration_data/train/c.csv", size=2000, age_days=6, verified=True)
        cap = 5000 / GB  # 6000 total -> drop oldest (2000) to reach 4000
        rep = cache_gc.run_gc(root, max_gb=cap, max_age_days=365, recency_floor_hours=24, now=NOW)
        assert rep.evicted_units == ["10/dev/03.01.2024"]
        assert rep.freed_bytes == 2000
        assert not (root / "10/dev/03.01.2024").exists()
        assert (root / "10/dev/03.03.2024").exists()


def test_cap_cannot_evict_unverified_even_under_pressure():
    """Cap pressure must never force deletion of un-verified data."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _mkfile(root, "10/dev/03.01.2024/calibration_data/train/a.csv", size=4000, age_days=10, verified=False)
        _mkfile(root, "10/dev/03.02.2024/calibration_data/train/b.csv", size=2000, age_days=8, verified=True)
        rep = cache_gc.run_gc(root, max_gb=1000 / GB, max_age_days=365, recency_floor_hours=24, now=NOW)
        # only the verified unit may go; the un-verified one stays even though
        # the cache is still over cap afterward
        assert rep.evicted_units == ["10/dev/03.02.2024"]
        assert (root / "10/dev/03.01.2024").exists()


def test_ttl_then_cap_combined():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _mkfile(root, "10/dev/01.01.2024/calibration_data/train/old.csv", size=2000, age_days=60, verified=True)
        _mkfile(root, "10/dev/03.02.2024/calibration_data/train/b.csv", size=2000, age_days=8, verified=True)
        _mkfile(root, "10/dev/03.03.2024/calibration_data/train/c.csv", size=2000, age_days=6, verified=True)
        rep = cache_gc.run_gc(root, max_gb=3000 / GB, max_age_days=28, recency_floor_hours=24, now=NOW)
        assert rep.evicted_units == ["10/dev/01.01.2024", "10/dev/03.02.2024"]
        assert (root / "10/dev/03.03.2024").exists()


def test_models_dir_is_a_unit():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _mkfile(root, "10/dev/models/03.12.2024/best_model/m.tflite", size=100, age_days=60, verified=True)
        _mkfile(root, "10/dev/models/03.12.2024/best_model/meta.json", size=50, age_days=60, verified=True)
        rep = cache_gc.run_gc(root, max_gb=1000, max_age_days=28, recency_floor_hours=24, now=NOW)
        assert rep.evicted_units == ["10/dev/models/03.12.2024"]
        assert not (root / "10/dev/models").exists()  # empty parents pruned


# ---------- throttle / switch ----------

def test_maybe_run_gc_throttles():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _mkfile(root, "10/dev/01.01.2024/calibration_data/train/old.csv", size=100, age_days=60, verified=True)
        cache_gc.maybe_run_gc(root, now=NOW, background=False, max_age_days=28, recency_floor_hours=24)
        assert not (root / "10/dev/01.01.2024").exists()
        _mkfile(root, "10/dev/01.01.2024/calibration_data/train/old.csv", size=100, age_days=60, verified=True)
        cache_gc.maybe_run_gc(root, now=NOW + 3600, background=False, max_age_days=28, recency_floor_hours=24)
        assert (root / "10/dev/01.01.2024").exists()  # throttled (12h window)
        cache_gc.maybe_run_gc(root, now=NOW + 13 * 3600, background=False, max_age_days=28, recency_floor_hours=24)
        assert not (root / "10/dev/01.01.2024").exists()


def test_disabled_via_env():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _mkfile(root, "10/dev/01.01.2024/calibration_data/train/old.csv", size=100, age_days=60, verified=True)
        os.environ[cache_gc.ENV_ENABLE] = "0"
        try:
            cache_gc.maybe_run_gc(root, now=NOW, background=False, max_age_days=1, recency_floor_hours=0)
            assert (root / "10/dev/01.01.2024").exists()
        finally:
            del os.environ[cache_gc.ENV_ENABLE]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
