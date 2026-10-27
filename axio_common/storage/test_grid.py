"""Characterization-grid listing and download (list_grid, download_grid_captures), both backends."""
import pytest

from axio_common.storage import storage_core as sc

DEV = "18-8c665420"
B = f"18/{DEV}/axiosaurus_characterization/"
KEYS = [
    B + "2026-10-20/18-8c665420-AC-CYLg729Ad4r4_10.20.2026.csv.gz",
    B + "2026-10-20/18-8c665420-AC-CYLg49Ad4r9_10.20.2026.csv.gz",
    B + "2026-10-20/axiosaurus/run_729.json",
    B + "2026-10-20/axiosaurus/run_729.gcode",
    B + "2026-10-21/18-8c665420-AC-CYLg729Ad2r8_10.21.2026.csv.gz",
    B + "2026-10-21/axiosaurus/run_1458.json",
    B + "2026-10-21/",                       # folder marker: never a capture
    B + "2026-10-21/notes.txt",             # neither capture nor sidecar
]


@pytest.fixture
def s3_backend(monkeypatch):
    monkeypatch.setattr(sc, "_use_server_backend", lambda: False)
    monkeypatch.setattr(sc, "list_prefix", lambda prefix, recursive=True: [k for k in KEYS if k.startswith(prefix)])


def test_list_grid_splits_captures_and_sidecars(s3_backend):
    g = sc.list_grid(DEV)
    assert g.captures == sorted(k for k in KEYS if k.endswith(".csv.gz"))
    assert g.sidecars == sorted(k for k in KEYS if "/axiosaurus/" in k)
    one = sc.list_grid(DEV, "2026-10-21")
    assert one.captures == [B + "2026-10-21/18-8c665420-AC-CYLg729Ad2r8_10.21.2026.csv.gz"]
    assert one.sidecars == g.sidecars          # every date's sidecars, always
    assert sc.list_grid_dates(DEV) == ["2026-10-20", "2026-10-21"]


def test_download_fetches_all_sidecars_and_returns_capture_paths(s3_backend, monkeypatch):
    got = []
    monkeypatch.setattr(sc, "download_files",
                        lambda keys, cache_root=None, **kw: got.extend(keys) or [f"P:{k}" for k in keys])
    want = [B + "2026-10-21/18-8c665420-AC-CYLg729Ad2r8_10.21.2026.csv.gz"]
    paths = sc.download_grid_captures(want)
    assert paths == [f"P:{want[0]}"]
    assert set(got) == set(want) | {k for k in KEYS if "/axiosaurus/" in k}


def test_download_refuses_missing_or_non_grid_keys(s3_backend, monkeypatch):
    monkeypatch.setattr(sc, "download_files", lambda *a, **k: pytest.fail("must not download"))
    with pytest.raises(FileNotFoundError):
        sc.download_grid_captures([B + "2026-10-20/not-there_10.20.2026.csv.gz"])
    with pytest.raises(ValueError):
        sc.download_grid_captures([f"18/{DEV}/2026-10-15/train/x.csv.gz"])


def test_server_backend_walks_files_endpoint(monkeypatch):
    """/storage/files lists one level; the walk must descend every prefix."""
    monkeypatch.setattr(sc, "_use_server_backend", lambda: True)
    from urllib.parse import parse_qs, urlparse

    def fake(path):
        prefix = parse_qs(urlparse(path).query)["prefix"][0]
        level = [k for k in KEYS if k.startswith(prefix) and k != prefix]
        objs = [k for k in level if "/" not in k[len(prefix):]]
        pref = sorted({prefix + k[len(prefix):].split("/")[0] + "/" for k in level if "/" in k[len(prefix):]})
        return {"prefix": prefix, "prefixes": pref, "objects": [{"key": k} for k in objs], "truncated": False}
    monkeypatch.setattr(sc, "_server_get_json", fake)
    g = sc.list_grid(DEV)
    assert g.captures == sorted(k for k in KEYS if k.endswith(".csv.gz"))
    assert len(g.sidecars) == 3

    monkeypatch.setattr(sc, "_server_get_json", lambda p: {"prefixes": [], "objects": [], "truncated": True})
    with pytest.raises(RuntimeError):
        sc.list_grid(DEV)


def test_list_dates_offers_only_sessions(monkeypatch):
    """A dev kit's grid subtree is not a session date (it was, before 0.69.1)."""
    monkeypatch.setattr(sc, "_use_server_backend", lambda: False)
    monkeypatch.setattr(sc, "list_top_dirs",
                        lambda prefix="": ["2026-10-15", "2026-10-19", "axiosaurus_characterization", "models"])
    assert sc.list_dates(DEV) == ["2026-10-15", "2026-10-19", "models"]
