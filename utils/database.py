from db_core import Base, engine, SessionLocal
from logger_config import logger

# Dependency to get database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from models.client import Client
    print("Initializing database...")
    Base.metadata.create_all(bind=engine)
    logger.info("Database initialized with the following tables:")
    for table in Base.metadata.tables:
        logger.info(table)


if __name__ == "__main__":
    init_db()
