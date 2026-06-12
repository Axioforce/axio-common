"""Tests for the picker's parallel, cached device loading.

load_types_and_devices is a pure data helper (no Tk windows are created),
so these run headless; the storage calls are monkeypatched.
"""
import pytest

import axio_common.storage.picker as picker


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(
        picker, "_device_cache", {"ts": 0.0, "types": None, "devices": None}
    )


def _fake_listing(monkeypatch, types, devices_by_type, calls):
    def list_device_types():
        calls.append("types")
        return types

    def list_devices(t):
        calls.append(f"devices:{t}")
        return devices_by_type[t]

    monkeypatch.setattr(picker._sc, "list_device_types", list_device_types)
    monkeypatch.setattr(picker._sc, "list_devices", list_devices)


def test_loads_all_devices_sorted(monkeypatch):
    _fake_listing(
        monkeypatch, ["10", "11"],
        {"10": ["10-00000002", "10-00000001"], "11": ["11-00000009"]}, [],
    )
    types, devices = picker.load_types_and_devices(use_cache=False)
    assert types == ["10", "11"]
    assert devices == ["10-00000001", "10-00000002", "11-00000009"]


def test_callbacks_fire_per_type(monkeypatch):
    _fake_listing(
        monkeypatch, ["10", "11"],
        {"10": ["10-00000001"], "11": ["11-00000001"]}, [],
    )
    seen_types, chunks = [], []
    picker.load_types_and_devices(
        on_types=seen_types.append,
        on_devices_chunk=lambda t, devs, done, total: chunks.append(
            (t, list(devs), done, total)
        ),
        use_cache=False,
    )
    assert seen_types == [["10", "11"]]
    # One chunk per type, completion counter reaches the total
    assert sorted(c[0] for c in chunks) == ["10", "11"]
    assert sorted(c[2] for c in chunks) == [1, 2]
    assert all(c[3] == 2 for c in chunks)


def test_cache_hit_skips_network(monkeypatch):
    calls = []
    _fake_listing(monkeypatch, ["10"], {"10": ["10-00000001"]}, calls)
    picker.load_types_and_devices()
    n_first = len(calls)
    chunks = []
    types, devices = picker.load_types_and_devices(
        on_devices_chunk=lambda t, devs, done, total: chunks.append(t)
    )
    assert len(calls) == n_first  # no new network calls
    assert devices == ["10-00000001"]
    assert chunks == [None]  # cache hit delivers one full chunk


def test_use_cache_false_refetches(monkeypatch):
    calls = []
    _fake_listing(monkeypatch, ["10"], {"10": ["10-00000001"]}, calls)
    picker.load_types_and_devices()
    picker.load_types_and_devices(use_cache=False)
    assert calls.count("types") == 2


def test_expired_cache_refetches(monkeypatch):
    calls = []
    _fake_listing(monkeypatch, ["10"], {"10": ["10-00000001"]}, calls)
    picker.load_types_and_devices()
    monkeypatch.setattr(picker, "_DEVICE_CACHE_TTL", 0.0)
    picker.load_types_and_devices()
    assert calls.count("types") == 2
