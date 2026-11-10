"""Dev-kit release ledger (Dyno #1421): the table and the step-report state
machine. SQLite in memory."""
import pytest

import sqlalchemy

_orig = sqlalchemy.create_engine


def _patched(url, *args, **kwargs):
    if str(url).startswith('sqlite'):
        for k in ('max_overflow', 'pool_timeout', 'pool_size', 'connect_args'):
            kwargs.pop(k, None)
    return _orig(url, *args, **kwargs)


sqlalchemy.create_engine = _patched

from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from axio_common.database import Base  # noqa: E402
from axio_common.models import (  # noqa: E402
    MAX_RELEASE_ATTEMPTS, DevkitRelease, DevkitReleaseResponse,
    ReleaseReportRefused,
)

KIT = "18.a1421c01"


@pytest.fixture
def db():
    engine = _orig("sqlite://")
    Base.metadata.create_all(engine, tables=[DevkitRelease.__table__])
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _row(db, version="1.26.1110+1", verdict="active"):
    r = DevkitRelease(device_axf_id=KIT, flex_fp=KIT[3:], version=version,
                      dev_row_id=f"{KIT}/{version}", verdict=verdict,
                      reasons=["first production model"])
    db.add(r)
    db.commit()
    return r


def test_defaults_and_response(db):
    r = _row(db)
    assert r.state == "pending" and r.attempts == 0
    resp = DevkitReleaseResponse.model_validate(r)
    assert resp.reasons == ["first production model"]


def test_one_row_per_flex_and_version(db):
    _row(db)
    with pytest.raises(IntegrityError):
        _row(db)


def test_copy_then_seal(db):
    r = _row(db)
    assert r.apply_report("copy_open", "done",
                          production_row_id=f"{KIT}/1.26.1110+1")
    assert r.state == "copied" and r.production_row_id.endswith("+1")
    assert r.apply_report("seal_entry", "done", entry_sha256="ab" * 32,
                          status="active")
    assert (r.state, r.sealed_status, r.entry_sha256) == ("done", "active",
                                                          "ab" * 32)
    # A repeat success on a finished release changes nothing.
    assert r.apply_report("seal_entry", "already", status="active") is False


def test_already_counts_as_done(db):
    r = _row(db)
    r.apply_report("copy_open", "already")
    assert r.state == "copied"
    r.apply_report("seal_entry", "already", status=None)
    assert r.state == "done" and r.sealed_status is None


def test_failures_count_then_fail(db):
    r = _row(db)
    for n in range(1, MAX_RELEASE_ATTEMPTS):
        r.apply_report("copy_open", "failed", detail=f"boom {n}")
        assert r.state == "pending" and r.attempts == n
        assert r.last_error == f"boom {n}"
    r.apply_report("copy_open", "failed", detail="last")
    assert r.state == "failed"
    with pytest.raises(ReleaseReportRefused):
        r.apply_report("copy_open", "done")


def test_success_clears_last_error(db):
    r = _row(db)
    r.apply_report("seal_entry", "failed", detail="409 conflict")
    r.apply_report("copy_open", "done")
    assert r.last_error is None and r.attempts == 1 and r.state == "copied"


def test_failure_on_a_done_release_is_refused(db):
    r = _row(db)
    r.apply_report("seal_entry", "done", status="draft")
    with pytest.raises(ReleaseReportRefused):
        r.apply_report("seal_entry", "failed", detail="late")


@pytest.mark.parametrize("kw", [
    {"step": "sign", "outcome": "done"},
    {"step": "copy_open", "outcome": "maybe"},
    {"step": "seal_entry", "outcome": "done", "status": "live"},
])
def test_bad_values(db, kw):
    r = _row(db)
    step, outcome = kw.pop("step"), kw.pop("outcome")
    with pytest.raises(ValueError):
        r.apply_report(step, outcome, **kw)
    assert r.state == "pending"
