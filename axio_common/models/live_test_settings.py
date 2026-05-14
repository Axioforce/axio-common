"""SQLAlchemy model for FluxLite's shared Live Test defaults.

Single-row table — conventionally exactly one row exists, with id='global'.
PUTs upsert against that primary key. The JSONB `payload` is opaque to the
server; the renderer owns its shape. `schema_version` lets the renderer
detect breaking shape changes and fall through to factory defaults instead
of crashing on a renamed field.

Used by axio-server's /live-test/settings/defaults endpoints.
"""

from sqlalchemy import Column, String, Integer, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from axio_common.database import Base


class LiveTestSettings(Base):
    __tablename__ = "live_test_settings"

    id = Column(String, primary_key=True, default="global")
    schema_version = Column(Integer, nullable=False, default=1)
    payload = Column(JSONB, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
