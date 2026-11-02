"""Operator pin on a device's shipped-model pointer (Dyno #1305).

SQLite in memory: devices, jobs and runs only, no Postgres, no network. The
jobs table's Postgres ARRAY column is compiled as JSON for SQLite here.

What these pin down: `pin_best` sets the head exactly as
`recompute_best_metrics` would and stamps who/when; a better run completing
and a recompute both leave a pinned head alone; deleting the pinned job
(soft through `Job.update_status`, or hard, then a recompute) drops the pin so
a deleted model is never shipped; `unpin_best` recomputes; and every refusal
writes nothing.
"""
import pytest

original_create_engine = None


def patched_create_engine(url, *args, **kwargs):
    if str(url).startswith('sqlite'):
        for k in ('max_overflow', 'pool_timeout', 'pool_size', 'connect_args'):
            kwargs.pop(k, None)
    return original_create_engine(url, *args, **kwargs)


import sqlalchemy

original_create_engine = sqlalchemy.create_engine
sqlalchemy.create_engine = patched_create_engine

from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker


@compiles(ARRAY, "sqlite")
def _array_as_json(type_, compiler, **kw):
    return "JSON"


from axio_common.database import Base
from axio_common.models import BestPinNotFound, BestPinRefused, Device, Job, Run
from axio_common.models.run import CompleteRunRequest

DEV = "10.b1e57a01"
OTHER = "10.b1e57a02"


def metrics(fz, mx=1.0):
    """TE-all MAE: index 0 ranks moment (x), index -1 ranks force (z)."""
    return {"TE-all": {"mae": [mx, 0.5, fz]}}


@pytest.fixture
def db():
    engine = original_create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        Device.__table__, Job.__table__, Run.__table__])
    s = sessionmaker(bind=engine)()
    s.add(Device(axf_id=DEV))
    s.add(Device(axf_id=OTHER))
    s.flush()
    try:
        yield s
    finally:
        s.close()


def job(db, ts, model_type="force", status="completed", device=DEV):
    j = Job(id=f"job-{device}-{ts}-{model_type}", device_axf_id=device,
            model_type=model_type, config="{}", status=status, timestamp=ts)
    db.add(j)
    db.flush()
    return j


def run(db, j, number, test_metrics):
    r = Run(job_id=j.id, number=number, test_metrics=test_metrics,
            train_metrics={"tag": f"train {j.timestamp}/{number}"},
            val_metrics={"tag": f"val {j.timestamp}/{number}"})
    db.add(r)
    db.flush()
    return r


def device(db, axf=DEV):
    return db.query(Device).filter(Device.axf_id == axf).one()


def head(d, model_type="force"):
    return (getattr(d, f"best_{model_type}_timestamp"),
            getattr(d, f"best_{model_type}_run"))


@pytest.fixture
def fleet(db):
    """Device with a low-MAE force job (A) and a worse thermal retrain (B)."""
    a = job(db, 20261001120000)
    run(db, a, 1, metrics(1.0))
    b = job(db, 20261101120000)
    run(db, b, 1, metrics(2.0))
    run(db, b, 2, metrics(1.5))
    d = device(db)
    d.recompute_best_metrics(db)
    assert head(d) == (20261001120000, 1)
    return {"a": a, "b": b, "d": d}


def test_pin_sets_the_five_fields_and_the_pin(db, fleet):
    d = fleet["d"]
    r = d.pin_best(db, "force", 20261101120000, 2, "admin (Stephen)", note="thermal #1299")
    assert head(d) == (20261101120000, 2)
    assert d.best_force_test_metrics == r.test_metrics
    assert d.best_force_train_metrics == r.train_metrics
    assert d.best_force_val_metrics == r.val_metrics
    assert d.best_force_pinned_at is not None
    assert d.best_force_pinned_by == "admin (Stephen)"
    assert d.best_force_pin_note == "thermal #1299"
    bm = d.best_metrics()
    assert bm["force"]["pinned"] is True
    assert bm["force"]["pinned_by"] == "admin (Stephen)"
    assert bm["force"]["run"] == 2
    assert bm["moment"]["pinned"] is False
    assert d.is_pinned("force") and not d.is_pinned("moment")


def test_pinned_head_matches_what_recompute_would_write(db, fleet):
    """Pin the run recompute picks: every pointer field must be identical."""
    d = fleet["d"]
    before = {k: getattr(d, k) for k in (
        "best_force_run", "best_force_timestamp", "best_force_train_metrics",
        "best_force_val_metrics", "best_force_test_metrics")}
    d.pin_best(db, "force", 20261001120000, 1, "admin")
    after = {k: getattr(d, k) for k in before}
    assert after == before


def test_better_completion_does_not_move_a_pinned_head(db, fleet):
    d = fleet["d"]
    d.pin_best(db, "force", 20261101120000, 2, "admin")
    c = job(db, 20261102120000, status="running")
    r = Run(job_id=c.id, number=1)
    db.add(r)
    db.flush()
    update = CompleteRunRequest(job_id=c.id, run_number=1, train_metrics={},
                                val_metrics={}, test_metrics=metrics(0.1),
                                epochs_completed=10, hostname="h")
    r.complete(c, update, db)
    db.flush()
    assert r.is_best  # it is still its job's best run
    assert head(device(db)) == (20261101120000, 2)
    assert device(db).is_pinned("force")


def test_completion_still_moves_the_unpinned_head(db, fleet):
    d = fleet["d"]
    d.pin_best(db, "force", 20261101120000, 2, "admin")
    m = job(db, 20261103120000, model_type="moment", status="running")
    r = Run(job_id=m.id, number=1)
    db.add(r)
    db.flush()
    update = CompleteRunRequest(job_id=m.id, run_number=1, train_metrics={},
                                val_metrics={}, test_metrics=metrics(9.0, mx=0.3),
                                epochs_completed=10, hostname="h")
    r.complete(m, update, db)
    db.flush()
    assert head(device(db), "moment") == (20261103120000, 1)
    assert head(device(db)) == (20261101120000, 2)


def test_recompute_does_not_move_a_pinned_head(db, fleet):
    d = fleet["d"]
    d.pin_best(db, "force", 20261101120000, 2, "admin")
    d.recompute_best_metrics(db)
    assert head(d) == (20261101120000, 2)
    assert d.is_pinned("force")


def test_soft_deleting_the_pinned_job_unpins_and_recomputes(db, fleet):
    d = fleet["d"]
    d.pin_best(db, "force", 20261101120000, 2, "admin")
    fleet["b"].update_status("deleted", db)
    d = device(db)
    assert not d.is_pinned("force")
    assert d.best_force_pinned_by is None and d.best_force_pin_note is None
    assert head(d) == (20261001120000, 1)


def test_hard_deleting_the_pinned_job_unpins_on_recompute(db, fleet):
    d = fleet["d"]
    d.pin_best(db, "force", 20261101120000, 2, "admin")
    db.query(Run).filter(Run.job_id == fleet["b"].id).delete(synchronize_session="fetch")
    db.query(Job).filter(Job.id == fleet["b"].id).delete(synchronize_session="fetch")
    db.flush()
    d.recompute_best_metrics(db)
    assert not d.is_pinned("force")
    assert head(d) == (20261001120000, 1)


def test_deleting_another_job_keeps_the_pin(db, fleet):
    d = fleet["d"]
    d.pin_best(db, "force", 20261101120000, 2, "admin")
    fleet["a"].update_status("deleted", db)
    d = device(db)
    assert d.is_pinned("force")
    assert head(d) == (20261101120000, 2)


def test_unpin_recomputes_the_head(db, fleet):
    d = fleet["d"]
    d.pin_best(db, "force", 20261101120000, 2, "admin")
    d.unpin_best(db, "force")
    assert not d.is_pinned("force")
    assert head(d) == (20261001120000, 1)
    assert d.best_metrics()["force"]["pinned"] is False


def test_unpin_touches_only_its_head(db, fleet):
    d = fleet["d"]
    m = job(db, 20261104120000, model_type="moment")
    run(db, m, 1, metrics(9.0, mx=0.2))
    d.pin_best(db, "moment", 20261104120000, 1, "admin")
    d.pin_best(db, "force", 20261101120000, 2, "admin")
    d.unpin_best(db, "force")
    assert d.is_pinned("moment")
    assert head(d, "moment") == (20261104120000, 1)


def test_repin_moves_the_pin(db, fleet):
    d = fleet["d"]
    d.pin_best(db, "force", 20261101120000, 2, "admin")
    d.pin_best(db, "force", 20261101120000, 1, "admin (other)")
    assert head(d) == (20261101120000, 1)
    assert d.best_force_pinned_by == "admin (other)"


def _refused(db, d, exc, *args, **kwargs):
    before = (head(d), d.best_force_pinned_at, d.best_force_pinned_by)
    with pytest.raises(exc):
        d.pin_best(db, *args, **kwargs)
    assert (head(d), d.best_force_pinned_at, d.best_force_pinned_by) == before


def test_refusals_write_nothing(db, fleet):
    d = fleet["d"]
    queued = job(db, 20261105120000, status="running")
    run(db, queued, 1, metrics(0.1))
    gone = job(db, 20261106120000, status="deleted")
    run(db, gone, 1, metrics(0.1))
    bare = job(db, 20261107120000)
    run(db, bare, 1, {"TE-1": {"mae": [0.1, 0.1, 0.1]}})
    theirs = job(db, 20261108120000, device=OTHER)
    run(db, theirs, 1, metrics(0.1))
    moment = job(db, 20261109120000, model_type="moment")
    run(db, moment, 1, metrics(0.1))

    _refused(db, d, BestPinRefused, "cop", 20261101120000, 2, "admin")
    _refused(db, d, BestPinRefused, "force", 20261101120000, 2, "  ")
    _refused(db, d, BestPinRefused, "force", "not-a-uid", 2, "admin")
    _refused(db, d, BestPinRefused, "force", 20261105120000, 1, "admin")   # not completed
    _refused(db, d, BestPinRefused, "force", 20261106120000, 1, "admin")   # deleted
    _refused(db, d, BestPinRefused, "force", 20261107120000, 1, "admin")   # no TE-all
    _refused(db, d, BestPinRefused, "force", 20261109120000, 1, "admin")   # a moment job
    _refused(db, d, BestPinNotFound, "force", 20261108120000, 1, "admin")  # other device's job
    _refused(db, d, BestPinNotFound, "force", 20991231000000, 1, "admin")  # no such job
    _refused(db, d, BestPinNotFound, "force", 20261101120000, 9, "admin")  # no such run
    with pytest.raises(BestPinRefused):
        d.unpin_best(db, "cop")
