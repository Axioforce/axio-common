"""Tests for the cache<->bucket key mapping and the upload-path guard.

Covers the inverse `bucket_key_for_cache_path` (added after the 2026-07 restore
incident where cache paths were uploaded verbatim as bucket keys, leaking a
dotted date + a cache-only 'calibration_data/' layer into the bucket) and the
`_assert_canonical_key` guard that now rejects such keys.
"""
import pytest

from axio_common.storage import storage_core as sc


CANONICAL_KEYS = [
    "10/10-00000002/2024-12-20/train/10-00000002-TR-BER_12.20.2024.csv.gz",
    "16/16-00000001/2026-07-27/test/16-00000001-TE-45DBH_07.27.2026.csv.gz",
    "16/16-00000001/2026-07-27/tests.txt",
    "16/16-00000001/models/07.27.2026/nn_results/force_vector/x/16-00000001-y-f.h5",
    "18/18-8c665420/axiosaurus_characterization/2026-10-20/18-8c665420-AC-CYLg729Ad4r4_10.20.2026.csv.gz",
    "18/18-8c665420/axiosaurus_characterization/2026-10-20/axiosaurus/"
    "18-8c665420-AC-cylindergrid-CYLg729Ad4r4_10.20.2026_729.json",
]


def test_grid_capture_cache_name_is_gunzipped(tmp_path):
    """ensure_local gunzips every .csv.gz, so a grid capture's cache file must be
    named .csv (it kept .csv.gz before 0.69.0: plain CSV under a gzip name)."""
    key = "18/18-8c665420/axiosaurus_characterization/2026-10-20/x_10.20.2026.csv.gz"
    p = sc.cache_path_for_key(key, cache_root=tmp_path)
    assert p == tmp_path / "18/18-8c665420/axiosaurus_characterization/10.20.2026/x_10.20.2026.csv"
    side = "18/18-8c665420/axiosaurus_characterization/2026-10-20/axiosaurus/r.json"
    assert (sc.cache_path_for_key(side, cache_root=tmp_path)
            == tmp_path / "18/18-8c665420/axiosaurus_characterization/10.20.2026/axiosaurus/r.json")


@pytest.mark.parametrize("key", CANONICAL_KEYS)
def test_roundtrip_bucket_cache_bucket(key, tmp_path):
    """bucket -> cache -> bucket returns the original canonical key exactly."""
    cache_path = sc.cache_path_for_key(key, cache_root=tmp_path)
    back = sc.bucket_key_for_cache_path(cache_path, cache_root=tmp_path)
    assert back == key


def test_inverse_fixes_the_restore_bug():
    """The exact mis-restore shape maps back to the correct ISO bucket key."""
    # .csv.gz variant (what the incident actually uploaded)
    wrong = ("16/16-00000001/07.27.2026/calibration_data/test/"
             "16-00000001-TE-45DBH_07.27.2026.csv.gz")
    assert (sc.bucket_key_for_cache_path(wrong)
            == "16/16-00000001/2026-07-27/test/16-00000001-TE-45DBH_07.27.2026.csv.gz")
    # gunzipped .csv variant (what cache_path_for_key produces) -> .csv.gz key
    wrong_csv = ("10/10-00000002/12.20.2024/calibration_data/train/"
                 "10-00000002-TR-BER_12.20.2024.csv")
    assert (sc.bucket_key_for_cache_path(wrong_csv)
            == "10/10-00000002/2024-12-20/train/10-00000002-TR-BER_12.20.2024.csv.gz")


def test_inverse_leaves_models_and_filename_dates_untouched():
    models = "16/16-00000001/models/07.27.2026/nn_results/f/16-00000001-z-f.h5"
    assert sc.bucket_key_for_cache_path(models) == models  # verbatim, still dotted


@pytest.mark.parametrize("bad", [
    "16/16-00000001/07.27.2026/calibration_data/test/x.csv.gz",  # calibration_data layer
    "16/16-00000001/07.27.2026/test/x.csv.gz",                   # dotted (non-ISO) session date
    "16/16-00000001/07.27.2026/calibration_data/train/x.csv",
])
def test_guard_rejects_noncanonical(bad):
    with pytest.raises(ValueError):
        sc._assert_canonical_key(bad)


@pytest.mark.parametrize("ok", [
    "16/16-00000001/2026-07-27/test/16-00000001-TE-45DBH_07.27.2026.csv.gz",
    "16/16-00000001/2026-07-27/train/x.csv.gz",
    "16/16-00000001/2026-07-27/tests.txt",
    "16/16-00000001/models/07.27.2026/nn_results/f/x-f.h5",   # models: dotted date OK
    "_checkpoints/16-00000001/some-job/run_5/weights.h5",     # checkpoints: not a session
    "_audit/hard_deletes/20260728__16-00000001__2026-07-27.json",
])
def test_guard_allows_canonical(ok):
    sc._assert_canonical_key(ok)  # must not raise
