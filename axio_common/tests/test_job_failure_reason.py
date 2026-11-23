"""A job's failure reason through requeue and completion.

SQLite in memory, jobs only. The reason an attempt ended stays on the job while
it is requeued and running again (the dashboard shows why it was requeued), and
is cleared when the job completes: a completed job carries no failure reason.
"""
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
from axio_common.models import Job

REASON = "Stopped and requeued on host-a by the operator"


@pytest.fixture
def db():
    engine = original_create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Job.__table__])
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _job(db):
    j = Job(id="job-reason", device_axf_id="10.b1e57a01", model_type="force", config="{}", status="running",
            timestamp=20261123000000)
    db.add(j)
    db.flush()
    return j


def test_reason_is_kept_while_requeued_and_running_then_cleared_on_completion(db):
    j = _job(db)
    j.update_status("interrupted", db, reason=REASON)
    j.update_status("queued", db)
    assert j.failure_reason == REASON and j.failure_count == 1
    j.status = "running"  # fetched and started again
    assert j.failure_reason == REASON
    j.update_status("completed", db)
    assert j.failure_reason is None
    assert j.failure_count == 1  # the record that it was requeued stays


def test_a_failed_job_keeps_its_reason(db):
    j = _job(db)
    j.update_status("failed", db, reason="refused: no rows")
    assert j.failure_reason == "refused: no rows"
