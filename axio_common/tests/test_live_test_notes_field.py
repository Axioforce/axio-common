"""The live-test session `notes` column: one free-text operator note per
session, nullable, mutable after save via PATCH.

FluxLite captures setup context before a test (mount, dumbbell, why this run)
and observations appended at the summary. Deliberately unbounded Text — a note
is more useful intact than truncated.
"""

# Patch create_engine before importing axio_common — SQLite rejects the pool
# kwargs axio_common.database passes at import time. Same preamble as
# test_bucket_session_fields.py.
original_create_engine = None


def patched_create_engine(url, *args, **kwargs):
    """Remove SQLite-incompatible pool parameters."""
    if str(url).startswith('sqlite'):
        kwargs.pop('max_overflow', None)
        kwargs.pop('pool_timeout', None)
        kwargs.pop('pool_size', None)
        kwargs.pop('connect_args', None)
    return original_create_engine(url, *args, **kwargs)


import sqlalchemy

original_create_engine = sqlalchemy.create_engine
sqlalchemy.create_engine = patched_create_engine

from axio_common.models.live_test import LiveTestSession


def test_notes_column_exists_and_is_nullable():
    cols = LiveTestSession.__table__.columns
    assert "notes" in cols
    assert cols["notes"].nullable is True


def test_notes_is_unbounded_text_not_capped_string():
    """Text, not String(n) — no truncation cliff at the storage layer."""
    col_type = LiveTestSession.__table__.columns["notes"].type
    assert isinstance(col_type, sqlalchemy.Text)
    assert getattr(col_type, "length", None) is None
