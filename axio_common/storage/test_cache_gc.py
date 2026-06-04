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


def _mkfile(root: Path, rel: str, *, size: int, age_days: float) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    mt = NOW - age_days * DAY
    os.utime(p, (mt, mt))
    return p


def _all_present(keys):
    """bucket_index that confirms everything is in the bucket."""
    return set(keys)


def _none_present(keys):
    return set()


# ---------- path -> key mapping ----------

def test_bucket_key_mapping():
    assert cache_gc._bucket_key_for(
        ("10", "10-00000002", "12.20.2024", "calibration_data", "train", "a.csv")
    ) == "10/10-00000002/2024-12-20/train/a.csv.gz"
    assert cache_gc._bucket_key_for(
        ("10", "10-00000002", "12.20.2024", "tests.txt")
    ) == "10/10-00000002/2024-12-20/tests.txt"
    assert cache_gc._bucket_key_for(
        ("10", "10-00000002", "models", "12.20.31.2024", "best_model", "m.tflite")
    ) == "10/10-00000002/models/12.20.31.2024/best_model/m.tflite"


def test_unit_detection():
    assert cache_gc._unit_parts(
        ("10", "dev", "12.20.2024", "calibration_data", "train", "a.csv")
    ) == ("10", "dev", "12.20.2024")
    assert cache_gc._unit_parts(
        ("10", "dev", "models", "compound", "best_model", "m.tflite")
    ) == ("10", "dev", "models", "compound")
    # a stray file with no recognizable unit is left alone
    assert cache_gc._unit_parts(("README.txt",)) is None


# ---------- TTL stage ----------

def test_ttl_evicts_old_confirmed_only():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _mkfile(root, "10/dev/01.01.2024/calibration_data/train/old.csv", size=100, age_days=60)
        _mkfile(root, "10/dev/06.01.2024/calibration_data/train/new.csv", size=100, age_days=2)
        rep = cache_gc.run_gc(root, _all_present, max_gb=1000, max_age_days=28, recency_floor_hours=24, now=NOW)
        assert rep.evicted_units == ["10/dev/01.01.2024"]
        assert not (root / "10/dev/01.01.2024").exists()
        assert (root / "10/dev/06.01.2024").exists()


def test_pending_never_evicted_even_when_old():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _mkfile(root, "10/dev/01.01.2024/calibration_data/train/old.csv", size=100, age_days=60)
        rep = cache_gc.run_gc(root, _none_present, max_gb=0, max_age_days=1, recency_floor_hours=0, now=NOW)
        assert rep.evicted_units == []
        assert rep.pending_files == 1
        assert (root / "10/dev/01.01.2024").exists()  # un-uploaded data preserved


def test_recency_floor_protects_fresh():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # old by TTL but touched 1h ago -> in-flight, must survive
        _mkfile(root, "10/dev/01.01.2024/calibration_data/train/x.csv", size=100, age_days=0.04)
        rep = cache_gc.run_gc(root, _all_present, max_gb=0, max_age_days=0, recency_floor_hours=24, now=NOW)
        assert rep.evicted_units == []
        assert (root / "10/dev/01.01.2024").exists()


# ---------- cap stage ----------

def test_cap_evicts_oldest_first_until_under():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # three confirmed sessions, 2000 B each, all past recency floor, none past TTL
        _mkfile(root, "10/dev/03.01.2024/calibration_data/train/a.csv", size=2000, age_days=10)
        _mkfile(root, "10/dev/03.02.2024/calibration_data/train/b.csv", size=2000, age_days=8)
        _mkfile(root, "10/dev/03.03.2024/calibration_data/train/c.csv", size=2000, age_days=6)
        cap = 5000 / GB  # cap below 6000 total -> must drop the oldest (2000) to reach 4000
        rep = cache_gc.run_gc(root, _all_present, max_gb=cap, max_age_days=365, recency_floor_hours=24, now=NOW)
        assert rep.evicted_units == ["10/dev/03.01.2024"]
        assert rep.freed_bytes == 2000
        assert not (root / "10/dev/03.01.2024").exists()
        assert (root / "10/dev/03.02.2024").exists()
        assert (root / "10/dev/03.03.2024").exists()


def test_ttl_then_cap_combined():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _mkfile(root, "10/dev/01.01.2024/calibration_data/train/old.csv", size=2000, age_days=60)  # TTL
        _mkfile(root, "10/dev/03.02.2024/calibration_data/train/b.csv", size=2000, age_days=8)
        _mkfile(root, "10/dev/03.03.2024/calibration_data/train/c.csv", size=2000, age_days=6)
        # TTL drops old (->4000 left); cap 3000 forces dropping next-oldest (b)
        rep = cache_gc.run_gc(root, _all_present, max_gb=3000 / GB, max_age_days=28, recency_floor_hours=24, now=NOW)
        assert rep.evicted_units == ["10/dev/01.01.2024", "10/dev/03.02.2024"]
        assert (root / "10/dev/03.03.2024").exists()


def test_models_dir_is_a_unit():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _mkfile(root, "10/dev/models/03.12.2024/best_model/m.tflite", size=100, age_days=60)
        _mkfile(root, "10/dev/models/03.12.2024/best_model/meta.json", size=50, age_days=60)
        rep = cache_gc.run_gc(root, _all_present, max_gb=1000, max_age_days=28, recency_floor_hours=24, now=NOW)
        assert rep.evicted_units == ["10/dev/models/03.12.2024"]
        assert not (root / "10/dev/models").exists()  # empty parents pruned


# ---------- throttle ----------

def test_maybe_run_gc_throttles():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _mkfile(root, "10/dev/01.01.2024/calibration_data/train/old.csv", size=100, age_days=60)
        # first call (no stamp) runs inline and evicts
        cache_gc.maybe_run_gc(root, _all_present, now=NOW, background=False, max_age_days=28, recency_floor_hours=24)
        assert not (root / "10/dev/01.01.2024").exists()
        # recreate; a call 1h later must be throttled (default 12h window)
        _mkfile(root, "10/dev/01.01.2024/calibration_data/train/old.csv", size=100, age_days=60)
        cache_gc.maybe_run_gc(root, _all_present, now=NOW + 3600, background=False, max_age_days=28, recency_floor_hours=24)
        assert (root / "10/dev/01.01.2024").exists()  # not swept — throttled
        # a call past the window sweeps again
        cache_gc.maybe_run_gc(root, _all_present, now=NOW + 13 * 3600, background=False, max_age_days=28, recency_floor_hours=24)
        assert not (root / "10/dev/01.01.2024").exists()


def test_disabled_via_env(monkeypatch=None):
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _mkfile(root, "10/dev/01.01.2024/calibration_data/train/old.csv", size=100, age_days=60)
        os.environ[cache_gc.ENV_ENABLE] = "0"
        try:
            cache_gc.maybe_run_gc(root, _all_present, now=NOW, background=False, max_age_days=1, recency_floor_hours=0)
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
