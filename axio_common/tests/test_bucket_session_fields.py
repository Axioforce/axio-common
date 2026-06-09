import typing
import sys
from unittest.mock import patch

# Patch create_engine before importing axio_common to handle SQLite parameter incompatibility
original_create_engine = None

def patched_create_engine(url, *args, **kwargs):
    """Remove SQLite-incompatible pool parameters"""
    if str(url).startswith('sqlite'):
        kwargs.pop('max_overflow', None)
        kwargs.pop('pool_timeout', None)
        kwargs.pop('pool_size', None)
        kwargs.pop('connect_args', None)
    return original_create_engine(url, *args, **kwargs)

import sqlalchemy
original_create_engine = sqlalchemy.create_engine
sqlalchemy.create_engine = patched_create_engine

from axio_common.models.bucket_session import (
    CalibrationBucketSession, CalibrationBucketSessionResponse,
)

def test_size_and_room_temp_columns_exist():
    cols = CalibrationBucketSession.__table__.columns
    assert "size" in cols and cols["size"].nullable is True
    assert "room_temp" in cols and cols["room_temp"].nullable is True

def test_response_dto_carries_size_and_room_temp():
    fields = CalibrationBucketSessionResponse.model_fields
    assert "size" in fields and "room_temp" in fields
    assert fields["size"].default is None
    assert fields["room_temp"].default is None
    for field_name, expected_inner in [("size", str), ("room_temp", float)]:
        args = typing.get_args(fields[field_name].annotation)
        assert expected_inner in args, f"{field_name} missing {expected_inner}"
        assert type(None) in args, f"{field_name} is not Optional"
