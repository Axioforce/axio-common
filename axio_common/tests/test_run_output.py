"""A run of another output than its job never competes for best (Dyno #1628).

A dev kit set recipe posts force, COP and field as runs 1 to 3 of one force job.
The COP and field runs carry their own figures (location error in mm, map error),
which must never be compared with the force run's MAE: not for the job's best
run, not for the device's best model, not on a recompute, not by a pin.
SQLite in memory.
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
from axio_common.models import Device, Devkit, Job, Run
from axio_common.models.device import BestPinRefused, current_devkit_recipe
from axio_common.models.run import CompleteRunRequest

KIT = "18.8c665420"
TS = 20261127120000
RUN_CONFIG = {"learning_rate": 0.001, "activation": "elu", "optimizer_name": "adam", "layers": [96, 96, 48],
              "batch_size": 256, "epochs": 300}
FORCE = {"TE-all": {"mae": [0.2, 0.2, 0.9]}}
# Lower than the force run's on any axis a careless comparison could read.
COP = {"TE-all": {"location_median_mm": [0.4], "location_p90_mm": [1.1], "mae": [0.1, 0.1, 0.1]}}
FIELD = {"TE-all": {"f_map_mae_n": [0.05]}}


@pytest.fixture
def db(monkeypatch):
    monkeypatch.delenv("DEVKIT_CURRENT_RECIPE", raising=False)
    engine = original_create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Device.__table__, Job.__table__, Run.__table__, Devkit.__table__])
    s = sessionmaker(bind=engine)()
    s.add(Device(axf_id=KIT))
    s.flush()
    try:
        yield s
    finally:
        s.close()


def kit_job(db, ts=TS):
    j = Job(id=f"job-{ts}", device_axf_id=KIT, model_type="force", status="running", timestamp=ts,
            config=json.dumps({"DEVKIT_RECIPE": current_devkit_recipe()}))
    db.add(j)
    db.flush()
    return j


def start(db, j, number, output=None):
    """A run starting as update_progress starts it (no commit: SQLite hands datetimes back naive)."""
    r = j.start_run(dict(RUN_CONFIG, **({"output": output} if output else {})), number)
    db.add(r)
    db.flush()
    db.info.setdefault("runs", []).append(r)     # held: a run loaded back from SQLite has naive datetimes


def complete(db, j, number, test_metrics):
    """The run completing through the call Job.complete_run makes."""
    r = db.query(Run).filter(Run.job_id == j.id, Run.number == number).one()
    r.complete(j, CompleteRunRequest(job_id=j.id, run_number=number, train_metrics={}, val_metrics={},
                                     test_metrics=test_metrics, epochs_completed=10, hostname="h"), db)
    db.flush()
    return r


def device(db):
    return db.query(Device).filter(Device.axf_id == KIT).one()


def head(d):
    return d.best_force_timestamp, d.best_force_run, d.best_force_test_metrics


def test_the_cop_and_field_runs_are_recorded_and_never_the_jobs_best(db):
    j = kit_job(db)
    for number, output in ((1, "force"), (2, "cop"), (3, "field")):
        start(db, j, number, output)
    # COP and field finish first: neither may become the job's best or the kit's model
    complete(db, j, 2, COP)
    complete(db, j, 3, FIELD)
    assert db.query(Run).filter(Run.job_id == j.id, Run.is_best).count() == 0
    assert head(device(db)) == (None, None, None)
    complete(db, j, 1, FORCE)
    runs = {r.number: r for r in db.query(Run).filter(Run.job_id == j.id)}
    assert [runs[n].output for n in (1, 2, 3)] == ["force", "cop", "field"]
    assert [runs[n].is_best for n in (1, 2, 3)] == [True, False, False]
    assert runs[2].test_metrics == COP and runs[3].test_metrics == FIELD and runs[2].completed_at
    assert head(device(db)) == (TS, 1, FORCE)
    assert runs[2].to_dict()["output"] == "cop"


def test_a_cop_run_finishing_after_force_leaves_the_force_run_best(db):
    j = kit_job(db)
    for number, output in ((1, "force"), (2, "cop")):
        start(db, j, number, output)
    complete(db, j, 1, FORCE)
    complete(db, j, 2, COP)                      # its "mae" is lower on every axis
    assert db.query(Run).filter(Run.job_id == j.id, Run.is_best).one().number == 1
    assert head(device(db)) == (TS, 1, FORCE)


def test_a_recompute_skips_them_and_a_pin_refuses_them(db):
    j = kit_job(db)
    for number, output in ((1, "force"), (2, "cop")):
        start(db, j, number, output)
    complete(db, j, 1, FORCE)
    complete(db, j, 2, COP)
    j.status = "completed"
    d = device(db)
    d._set_head("force", None)
    d.recompute_best_metrics(db)
    assert head(d) == (TS, 1, FORCE)
    with pytest.raises(BestPinRefused, match="cop run"):
        d.pin_best(db, "force", TS, 2, actor="a@b")
    assert d.pin_best(db, "force", TS, 1, actor="a@b").number == 1


def test_a_run_without_an_output_is_judged_as_before(db):
    j = kit_job(db)
    start(db, j, 1)                              # an older worker: no output in the run config
    complete(db, j, 1, FORCE)
    r = db.query(Run).filter(Run.job_id == j.id).one()
    assert r.output is None and r.is_best and head(device(db)) == (TS, 1, FORCE)
    # a second run naming the job's own output competes with it, as two force runs do
    start(db, j, 2, "force")
    complete(db, j, 2, {"TE-all": {"mae": [0.2, 0.2, 0.5]}})
    assert db.query(Run).filter(Run.job_id == j.id, Run.is_best).one().number == 2
    assert device(db).best_force_run == 2
