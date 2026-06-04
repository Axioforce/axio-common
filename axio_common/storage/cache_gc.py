"""
Garbage collection for the shared ~/.axio-cache calibration cache.

CANONICAL SOURCE: FlyNNServer/axio-common/axio_common/storage/cache_gc.py
This module is MIRRORED byte-for-byte into:
    AxioforceStack/AxioforceDynamoPy/app/storage/cache_gc.py
Keep the two copies identical. Everything here is backend-agnostic and
stdlib-only; the only host-specific piece is the `bucket_index` callback each
host injects (see each repo's storage adapter). If you change this file, change
both copies in the same PR.

Why this exists
---------------
The cache is dual-role: it is both the read-through download cache AND the
upload staging area for the DAQ. A freshly captured CSV lives ONLY in the cache
until it has been uploaded to Tigris. Deleting such a file would lose
calibration data permanently. Therefore the single inviolable rule is:

    NEVER delete a file whose bucket key is not confirmed present.

Anything in the cache that is not in the bucket is treated as un-uploaded and is
left untouched (and reported), so the upload-retry paths can deal with it. GC
only ever reclaims space that is provably re-downloadable.

Policy
------
One sweep, two stages, over confirmed-in-bucket units only (a "unit" is a whole
session date-dir or a whole model compound-dir; we evict at that granularity so
the date-folder walkers stay coherent and a re-download is a lazy no-op):

  1. TTL  — drop any unit whose newest file is older than MAX_AGE_DAYS.
  2. Cap  — if still over MAX_GB, evict oldest-first until under the cap.

Units touched within `recency_floor_hours` (default 24) are never evicted, so an
in-flight download or training run is never yanked out from under itself.

Trigger
-------
`maybe_run_gc()` is called from the Tigris read/write chokepoints. It is cheap
on the hot path: it reads a stamp file and returns immediately unless
GC_INTERVAL_HOURS have elapsed. When a sweep is due it runs in a background
daemon thread (guarded by a cache-wide lock file) so the triggering call never
blocks. Recency is keyed off mtime, not atime — NTFS last-access updates are
disabled by default on Windows, where the calibration desktops run.

Configuration (all optional; read at call time):
    AXIO_CALIBRATION_CACHE_GC                 master switch (default on; "0"/"false"/"no"/"off" disables)
    AXIO_CALIBRATION_CACHE_MAX_GB             hard cap, default 50
    AXIO_CALIBRATION_CACHE_MAX_AGE_DAYS       TTL, default 28 (4 weeks)
    AXIO_CALIBRATION_CACHE_GC_INTERVAL_HOURS  throttle window, default 12
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

# ---------- configuration ----------

ENV_ENABLE = "AXIO_CALIBRATION_CACHE_GC"
ENV_MAX_GB = "AXIO_CALIBRATION_CACHE_MAX_GB"
ENV_MAX_AGE_DAYS = "AXIO_CALIBRATION_CACHE_MAX_AGE_DAYS"
ENV_INTERVAL_HOURS = "AXIO_CALIBRATION_CACHE_GC_INTERVAL_HOURS"

DEFAULT_MAX_GB = 50.0
DEFAULT_MAX_AGE_DAYS = 28.0
DEFAULT_INTERVAL_HOURS = 12.0
DEFAULT_RECENCY_FLOOR_HOURS = 24.0
DEFAULT_LOCK_STALE_HOURS = 1.0

STAMP_NAME = ".gc_stamp"
LOCK_NAME = ".gc.lock"

DOTTED_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")


# ---------- small helpers ----------

def _log(logger: Optional[Callable[[str], None]], msg: str) -> None:
    if logger:
        try:
            logger(msg)
        except Exception:
            pass


def _truthy(val: str) -> bool:
    return val.strip().lower() not in ("0", "false", "no", "off", "")


def _enabled() -> bool:
    raw = os.environ.get(ENV_ENABLE)
    return True if raw is None else _truthy(raw)


def _resolve_float(explicit: Optional[float], env_name: str, default: float) -> float:
    if explicit is not None:
        return float(explicit)
    raw = os.environ.get(env_name)
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return default


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def _dotted_to_iso(dotted: str) -> str:
    """'12.20.2024' -> '2024-12-20'."""
    mo, d, y = dotted.split(".")
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def _bucket_key_for(parts: tuple[str, ...]) -> str:
    """Map a cache-relative path to its bucket key. Mirrors the inverse of the
    cache layout transforms shared by both storage modules:
      - drop any 'calibration_data' path segment
      - dotted date segments ('12.20.2024') -> ISO ('2024-12-20')
      - a trailing '.csv' becomes '.csv.gz' (the cache decompresses on download)
    """
    out = [p for p in parts if p != "calibration_data"]
    out = [_dotted_to_iso(p) if DOTTED_DATE_RE.match(p) else p for p in out]
    if out and out[-1].endswith(".csv"):
        out[-1] = out[-1] + ".gz"
    return "/".join(out)


def _unit_parts(parts: tuple[str, ...]) -> Optional[tuple[str, ...]]:
    """The eviction unit a cache file belongs to, as a tuple of path segments
    relative to the cache root, or None if the file isn't part of a recognizable
    session/model unit (in which case it is left alone).

      session: <type>/<device>/<dotted-date>
      model:   <type>/<device>/models/<compound>
    """
    if "models" in parts:
        idx = parts.index("models")
        # need type/device before models, and compound + a file after it
        if idx >= 2 and len(parts) >= idx + 3:
            return parts[: idx + 2]
        return None
    for i, p in enumerate(parts):
        if DOTTED_DATE_RE.match(p):
            if i >= 2 and len(parts) > i + 1:
                return parts[: i + 1]
            return None
    return None


def _prune_empty_parents(start: Path, root: Path) -> None:
    """Remove now-empty parent dirs up to (but not including) root."""
    cur = start
    root = root.resolve()
    try:
        while cur.resolve() != root and cur.is_dir() and not any(cur.iterdir()):
            cur.rmdir()
            cur = cur.parent
    except OSError:
        pass


# ---------- cache walk ----------

@dataclass
class _FileEntry:
    size: int
    mtime: float
    key: str
    unit: Optional[tuple[str, ...]]


def _walk(root: Path) -> list[_FileEntry]:
    entries: list[_FileEntry] = []
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if f.name.endswith(".part"):
            continue
        try:
            rel = f.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts
        # skip dotfiles/dot-dirs anywhere (covers .gc_stamp / .gc.lock)
        if any(p.startswith(".") for p in parts):
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        entries.append(
            _FileEntry(
                size=st.st_size,
                mtime=st.st_mtime,
                key=_bucket_key_for(parts),
                unit=_unit_parts(parts),
            )
        )
    return entries


# ---------- report ----------

@dataclass
class GCReport:
    ran: bool = False
    freed_bytes: int = 0
    evicted_units: list[str] = field(default_factory=list)
    pending_files: int = 0
    pending_bytes: int = 0
    kept_bytes: int = 0


# ---------- lock ----------

class _Lock:
    """Best-effort cross-process lock via O_EXCL file creation, with stale
    reclaim so a crashed sweep doesn't wedge GC forever."""

    def __init__(self, path: Path, *, now: float, stale_after_s: float):
        self.path = path
        self.now = now
        self.stale_after_s = stale_after_s
        self._held = False

    def _create(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        except OSError:
            return False
        try:
            os.write(fd, str(self.now).encode())
        finally:
            os.close(fd)
        self._held = True
        return True

    def acquire(self) -> bool:
        if self._create():
            return True
        try:
            age = self.now - self.path.stat().st_mtime
        except OSError:
            return False
        if age <= self.stale_after_s:
            return False
        # stale — reclaim
        try:
            self.path.unlink()
        except OSError:
            return False
        return self._create()

    def release(self) -> None:
        if not self._held:
            return
        try:
            self.path.unlink()
        except OSError:
            pass
        self._held = False


def _read_stamp(stamp: Path) -> float:
    try:
        return float(stamp.read_text().strip())
    except (OSError, ValueError):
        return 0.0


def _write_stamp(stamp: Path, now: float) -> None:
    try:
        stamp.write_text(str(now))
    except OSError:
        pass


# ---------- core sweep ----------

def run_gc(
    cache_root: str | Path,
    bucket_index: Callable[[Iterable[str]], Iterable[str]],
    *,
    max_gb: Optional[float] = None,
    max_age_days: Optional[float] = None,
    recency_floor_hours: float = DEFAULT_RECENCY_FLOOR_HOURS,
    now: Optional[float] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> GCReport:
    """Run one GC sweep synchronously and return a GCReport.

    bucket_index(keys) -> the subset of `keys` that exist in the bucket. The
    host injects this; GC stays backend-agnostic. If it raises, GC deletes
    nothing (we never evict what we couldn't confirm).
    """
    now = time.time() if now is None else now
    root = Path(cache_root)
    report = GCReport()
    if not root.is_dir():
        return report

    entries = _walk(root)
    if not entries:
        report.ran = True
        return report

    candidate_keys = {e.key for e in entries if e.unit is not None}
    present: set[str] = set()
    if candidate_keys:
        try:
            present = set(bucket_index(candidate_keys))
        except Exception as e:
            _log(logger, f"[cache-gc] bucket index failed; not evicting: {e}")
            return report

    max_gb = _resolve_float(max_gb, ENV_MAX_GB, DEFAULT_MAX_GB)
    max_age_days = _resolve_float(max_age_days, ENV_MAX_AGE_DAYS, DEFAULT_MAX_AGE_DAYS)
    cap_bytes = int(max_gb * (1024 ** 3))
    age_cutoff = now - max_age_days * 86400.0
    recency_cutoff = now - recency_floor_hours * 3600.0

    # group files into units
    units: dict[tuple[str, ...], dict] = {}
    for e in entries:
        if e.unit is None:
            continue  # unrecognized file — leave it, don't count against cap
        u = units.setdefault(
            e.unit, {"size": 0, "mtime": 0.0, "files": 0, "confirmed": True}
        )
        u["size"] += e.size
        u["mtime"] = max(u["mtime"], e.mtime)
        u["files"] += 1
        if e.key not in present:
            u["confirmed"] = False

    total_size = 0
    evictable: list[tuple[tuple[str, ...], dict]] = []
    for ukey, u in units.items():
        total_size += u["size"]
        if not u["confirmed"]:
            report.pending_files += u["files"]
            report.pending_bytes += u["size"]
            continue
        if u["mtime"] > recency_cutoff:
            continue  # too fresh — possibly in-flight
        evictable.append((ukey, u))

    evictable.sort(key=lambda kv: kv[1]["mtime"])  # oldest first

    to_evict: list[tuple[tuple[str, ...], dict]] = []
    survivors: list[tuple[tuple[str, ...], dict]] = []
    for ukey, u in evictable:
        if u["mtime"] < age_cutoff:
            to_evict.append((ukey, u))  # stage 1: TTL
        else:
            survivors.append((ukey, u))

    projected = total_size - sum(u["size"] for _, u in to_evict)
    for ukey, u in survivors:  # stage 2: cap (oldest first)
        if projected <= cap_bytes:
            break
        to_evict.append((ukey, u))
        projected -= u["size"]

    for ukey, u in to_evict:
        unit_dir = root.joinpath(*ukey)
        try:
            shutil.rmtree(unit_dir)
        except OSError as e:
            _log(logger, f"[cache-gc] failed to remove {unit_dir}: {e}")
            continue
        _prune_empty_parents(unit_dir.parent, root)
        report.evicted_units.append("/".join(ukey))
        report.freed_bytes += u["size"]

    report.kept_bytes = total_size - report.freed_bytes
    report.ran = True

    if report.evicted_units or report.pending_files:
        _log(
            logger,
            f"[cache-gc] freed {_fmt_bytes(report.freed_bytes)} across "
            f"{len(report.evicted_units)} unit(s); kept {_fmt_bytes(report.kept_bytes)}; "
            f"{report.pending_files} file(s)/{_fmt_bytes(report.pending_bytes)} pending "
            f"upload left in place",
        )
    return report


# ---------- throttled entry point ----------

def maybe_run_gc(
    cache_root: str | Path,
    bucket_index: Callable[[Iterable[str]], Iterable[str]],
    *,
    now: Optional[float] = None,
    logger: Optional[Callable[[str], None]] = None,
    background: bool = True,
    **run_kwargs,
):
    """Cheap, throttled GC trigger for the Tigris hot path.

    Returns None when disabled, throttled, or the lock is already held. When a
    sweep is launched it runs in a daemon thread (background=True, default) and
    this returns the Thread; with background=False the sweep runs inline and the
    GCReport is returned. Never raises — GC must not break a capture/download.
    """
    try:
        if not _enabled():
            return None
        now = time.time() if now is None else now
        root = Path(cache_root)
        interval_s = _resolve_float(None, ENV_INTERVAL_HOURS, DEFAULT_INTERVAL_HOURS) * 3600.0
        stamp = root / STAMP_NAME
        last = _read_stamp(stamp)
        if last and (now - last) < interval_s:
            return None
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        lock = _Lock(root / LOCK_NAME, now=now, stale_after_s=DEFAULT_LOCK_STALE_HOURS * 3600.0)
        if not lock.acquire():
            return None
        # Claim the throttle window up front so a burst of calls doesn't queue
        # behind the lock and re-trigger the instant the sweep finishes.
        _write_stamp(stamp, now)

        def _do():
            try:
                run_gc(root, bucket_index, now=now, logger=logger, **run_kwargs)
            except Exception as e:
                _log(logger, f"[cache-gc] sweep failed: {e}")
            finally:
                lock.release()

        if background:
            t = threading.Thread(target=_do, name="axio-cache-gc", daemon=True)
            t.start()
            return t
        _do()
        return None
    except Exception:
        return None
