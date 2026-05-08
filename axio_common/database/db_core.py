from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv
from axio_common.logger import logger
import os

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

Base = declarative_base()
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=3,
    pool_timeout=30,
    pool_recycle=1800,
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
