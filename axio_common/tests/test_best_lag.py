"""Lag-stacked models are kept out of a device's best pointer (Dyno #1595).

SQLite in memory: devices, jobs and runs only. A lag job (LAG_TAPS_MS in its
config) with the lowest MAE must not become the best model, on run completion
or on a recompute, until LAG_MODELS_IN_BEST=1; an existing pointer on a lag job
is moved off it by a recompute; a pin on a lag job is a person's choice and stays.
"""
import json

import pytest
import sqlalchemy

original_create_engine = sqlalchemy.create_engine


def patched_create_engine(url, *args, **kwargs):
    # axio_common.database builds its engine at import with Postgres pool arguments.
    if str(url).startswith('sqlite'):
        for k in ('max_overflow', 'pool_timeout', 'pool_size', 'connect_args'):
            kwargs.pop(k, None)
    return original_create_engine(url, *args, **kwargs)


sqlalchemy.create_engine = patched_create_engine

from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker


@compiles(ARRAY, "sqlite")
def _array_as_json(type_, compiler, **kw):
    return "JSON"


from axio_common.database import Base
from axio_common.models import Device, Job, Run
from axio_common.models.device import LAG_MODELS_IN_BEST_ENV
from axio_common.models.job import JobRequest
from axio_common.models.run import CompleteRunRequest

DEV = "11.b1595a01"
LAG = json.dumps({"LAG_TAPS_MS": [0, 10, 20, 40, 80, 160]})


def metrics(fz, mx=1.0):
    return {"TE-all": {"mae": [mx, 0.5, fz]}}


@pytest.fixture
def db(monkeypatch):
    monkeypatch.delenv(LAG_MODELS_IN_BEST_ENV, raising=False)
    engine = original_create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Device.__table__, Job.__table__, Run.__table__])
    s = sessionmaker(bind=engine)()
    s.add(Device(axf_id=DEV))
    s.flush()
    try:
        yield s
    finally:
        s.close()


def job(db, ts, config="{}", model_type="force"):
    j = Job(id=f"job-{ts}-{model_type}", device_axf_id=DEV, model_type=model_type, config=config,
            status="completed", timestamp=ts)
    db.add(j)
    db.flush()
    return j


def run(db, j, number, test_metrics):
    r = Run(job_id=j.id, number=number, test_metrics=test_metrics, train_metrics={}, val_metrics={})
    db.add(r)
    db.flush()
    return r


def head(d, model_type="force"):
    return getattr(d, f"best_{model_type}_timestamp"), getattr(d, f"best_{model_type}_run")


def device(db):
    return db.query(Device).filter(Device.axf_id == DEV).one()


def complete(db, j, number, test_metrics):
    """A run completing through the same call the server makes."""
    r = Run(job_id=j.id, number=number)
    db.add(r)
    db.flush()
    r.complete(j, CompleteRunRequest(job_id=j.id, run_number=number, train_metrics={}, val_metrics={},
                                     test_metrics=test_metrics, epochs_completed=10, hostname="h"), db)
    db.flush()
    return r


def test_a_recompute_skips_a_better_lag_job_and_moves_a_pointer_off_one(db):
    prod = job(db, 20261101120000)
    run(db, prod, 1, metrics(2.0))
    lag = job(db, 20261102120000, LAG)
    run(db, lag, 1, metrics(1.2))
    d = device(db)
    # the state production is in today: the pointer sits on the lag job
    d._set_head("force", db.query(Run).filter(Run.job_id == lag.id).one())
    assert head(d) == (20261102120000, 1)
    d.recompute_best_metrics(db)
    assert head(d) == (20261101120000, 1)


def test_a_lag_run_completing_leaves_the_pointer_alone(db):
    prod = job(db, 20261101120000)
    complete(db, prod, 1, metrics(2.0))
    d = device(db)
    assert head(d) == (20261101120000, 1)
    lag = job(db, 20261102120000, LAG)
    complete(db, lag, 1, metrics(1.2))
    assert head(d) == (20261101120000, 1)
    # the lag job still knows its own best run
    assert db.query(Run).filter(Run.job_id == lag.id, Run.is_best).count() == 1


def test_a_device_with_only_lag_jobs_has_no_best(db):
    lag = job(db, 20261102120000, LAG, model_type="moment")
    complete(db, lag, 1, metrics(1.0, mx=0.3))
    d = device(db)
    assert head(d, "moment") == (None, None)
    d.recompute_best_metrics(db)
    assert head(d, "moment") == (None, None)


def test_the_switch_lets_lag_models_compete(db, monkeypatch):
    prod = job(db, 20261101120000)
    run(db, prod, 1, metrics(2.0))
    lag = job(db, 20261102120000, LAG)
    run(db, lag, 1, metrics(1.2))
    monkeypatch.setenv(LAG_MODELS_IN_BEST_ENV, "1")
    d = device(db)
    d.recompute_best_metrics(db)
    assert head(d) == (20261102120000, 1)


def test_a_pin_on_a_lag_job_stays(db):
    prod = job(db, 20261101120000)
    run(db, prod, 1, metrics(2.0))
    lag = job(db, 20261102120000, LAG)
    run(db, lag, 1, metrics(1.2))
    d = device(db)
    d.pin_best(db, "force", 20261102120000, 1, "admin (test)")
    d.recompute_best_metrics(db)
    assert head(d) == (20261102120000, 1)


def test_an_empty_tap_list_or_bad_config_is_not_a_lag_job(db):
    a = job(db, 20261101120000, json.dumps({"LAG_TAPS_MS": []}))
    run(db, a, 1, metrics(1.0))
    b = job(db, 20261102120000, "not json")
    run(db, b, 1, metrics(0.9))
    d = device(db)
    d.recompute_best_metrics(db)
    assert head(d) == (20261102120000, 1)


def test_the_submit_request_takes_an_optional_priority():
    assert JobRequest(hostname="h", config={}).priority is None
    assert JobRequest(hostname="h", config={}, priority=-10).priority == -10
