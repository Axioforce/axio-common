"""SQLAlchemy ORM models for FluxLite's live-test session storage.

Three tables:
    live_test_sessions    one row per completed live-test session
    live_test_cells       one row per per-cell measurement
    live_test_aggregates  one row per (session, stage_type) aggregate

Sessions own cells and aggregates via FK; deletion cascades. The
`raw_object_key` on sessions plus `raw_t_start`/`raw_t_end` on cells
point at a single CSV in Tigris that holds the raw post-tare sensor
frames for each cell's ARMING+MEASURING window — used by FluxLite's
reprocessing flow to re-score a session against a different model.
"""

import uuid
from sqlalchemy import (
    Column, BigInteger, Integer, String, Boolean, Float,
    DateTime, ForeignKey, Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from axio_common.database import Base


STAGE_TYPES = ('dumbbell', 'two_leg', 'one_leg')
STAGE_LOCATIONS = ('A', 'B')
COLOR_BINS = ('green', 'light_green', 'yellow', 'orange', 'red')


class LiveTestSession(Base):
    __tablename__ = 'live_test_sessions'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    started_at = Column(DateTime(timezone=True), nullable=False, index=True)
    ended_at = Column(DateTime(timezone=True), nullable=False)

    device_id = Column(String, nullable=False, index=True)
    device_type = Column(String, nullable=False, index=True)
    model_id = Column(String, nullable=False)
    tester_name = Column(String, nullable=False)
    body_weight_n = Column(Float, nullable=False)

    grid_rows = Column(Integer, nullable=False)
    grid_cols = Column(Integer, nullable=False)
    n_cells_captured = Column(Integer, nullable=False)
    n_cells_expected = Column(Integer, nullable=False)

    overall_pass_rate = Column(Float, nullable=True)
    session_passed = Column(Boolean, nullable=True)

    app_version = Column(String, nullable=False)

    # Tigris object key for the raw-sensor CSV. Null when raw streaming
    # was disabled or the upload failed; the rest of the session still
    # saves successfully.
    raw_object_key = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    cells = relationship(
        'LiveTestCell', back_populates='session',
        cascade='all, delete-orphan', passive_deletes=True,
    )
    aggregates = relationship(
        'LiveTestAggregate', back_populates='session',
        cascade='all, delete-orphan', passive_deletes=True,
    )


class LiveTestCell(Base):
    __tablename__ = 'live_test_cells'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey('live_test_sessions.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )

    stage_index = Column(Integer, nullable=False)
    stage_name = Column(String, nullable=False)
    stage_type = Column(String, nullable=False)
    stage_location = Column(String, nullable=False)
    target_n = Column(Float, nullable=False)
    tolerance_n = Column(Float, nullable=False)

    row = Column(Integer, nullable=False)
    col = Column(Integer, nullable=False)

    mean_fz_n = Column(Float, nullable=False)
    std_fz_n = Column(Float, nullable=False)
    error_n = Column(Float, nullable=False)
    signed_error_n = Column(Float, nullable=False)
    error_ratio = Column(Float, nullable=False)
    color_bin = Column(String, nullable=False)
    # `pass` is reserved in Python; map the column name explicitly so
    # SQL stays `pass` while the attribute is `pass_`.
    pass_ = Column('pass', Boolean, nullable=False)
    captured_at = Column(DateTime(timezone=True), nullable=False)

    # Bracket the cell's slice of the Tigris raw CSV. Null when no raw
    # window was captured for this cell.
    raw_t_start = Column(DateTime(timezone=True), nullable=True)
    raw_t_end = Column(DateTime(timezone=True), nullable=True)

    session = relationship('LiveTestSession', back_populates='cells')

    __table_args__ = (
        Index(
            'ux_live_test_cells_natural',
            'session_id', 'stage_index', 'row', 'col',
            unique=True,
        ),
    )


class LiveTestAggregate(Base):
    __tablename__ = 'live_test_aggregates'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey('live_test_sessions.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )

    stage_type = Column(String, nullable=False)
    n_cells = Column(Integer, nullable=False)
    mae = Column(Float, nullable=True)
    signed_mean_error = Column(Float, nullable=True)
    std_error = Column(Float, nullable=True)
    pass_rate = Column(Float, nullable=True)

    session = relationship('LiveTestSession', back_populates='aggregates')

    __table_args__ = (
        Index(
            'ux_live_test_aggregates_natural',
            'session_id', 'stage_type',
            unique=True,
        ),
    )
