import sqlalchemy

_original_create_engine = sqlalchemy.create_engine


def _patched_create_engine(url, *args, **kwargs):
    """Remove SQLite-incompatible pool parameters."""
    if str(url).startswith("sqlite"):
        kwargs.pop("max_overflow", None)
        kwargs.pop("pool_timeout", None)
        kwargs.pop("pool_size", None)
        kwargs.pop("connect_args", None)
    return _original_create_engine(url, *args, **kwargs)


sqlalchemy.create_engine = _patched_create_engine

from axio_common.models.calibration_procedure import CalibrationActivityDayOverride


def test_day_override_has_nullable_order_index():
    cols = CalibrationActivityDayOverride.__table__.columns
    assert "order_index" in cols and cols["order_index"].nullable is True
