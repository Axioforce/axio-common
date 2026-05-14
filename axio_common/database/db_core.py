from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv
from axio_common.logger import logger
import os

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

Base = declarative_base()

# psycopg2 TCP keepalives so idle connections don't get silently killed by
# Fly.io / managed-postgres middleboxes. Without these we'd see transient
# "SSL SYSCALL error: Success" from sockets that the kernel still thinks
# are open. `keepalives_idle=60` starts probing after 1 min idle; 5 probes
# 30s apart catches a dropped link inside 3.5 minutes — well under the
# 4-minute bucket sync cycle.
_KEEPALIVE_ARGS = {
    "keepalives": 1,
    "keepalives_idle": 60,
    "keepalives_interval": 30,
    "keepalives_count": 5,
}
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=3,
    pool_timeout=30,
    # 10-min recycle. Long-lived connections held by the bucket sync would
    # otherwise sit idle for up to 4 minutes between cycles; recycling more
    # aggressively keeps us inside any infrastructure idle-kill window.
    pool_recycle=600,
    connect_args=_KEEPALIVE_ARGS if (DATABASE_URL or "").startswith(
        ("postgresql://", "postgres://")
    ) else {},
)
SessionLocal = sessionmaker(bind=engine)

# Create all tables. Don't crash module import if the DB is unreachable —
# that prevents the FastAPI app from starting at all, which kills routes
# (storage, submit-defaults, health) that don't even need the DB. In prod
# the schema is migrated by alembic; this call is mostly a safety net for
# fresh local dev environments.
try:
    Base.metadata.create_all(bind=engine)
except Exception as _e:
    logger.warning(
        f"axio_common.database: skipped create_all at import time: {_e}. "
        "Endpoints that hit the DB will still error until the connection is restored."
    )

# Dependency to get database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
