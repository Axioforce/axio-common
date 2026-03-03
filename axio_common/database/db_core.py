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

# Create all tables
Base.metadata.create_all(bind=engine)

# Dependency to get database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
