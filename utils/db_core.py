import firebase_admin
from firebase_admin import credentials, firestore
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv
from logger_config import logger
import os

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
FIREBASE_CREDENTIALS = os.getenv("FIREBASE_CREDENTIALS")
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL")
FIREBASE_BUCKET = os.getenv("FIREBASE_BUCKET")
SERVER_NAME = os.getenv("SERVER_NAME")

# logger.info(f"Using database URL: {DATABASE_URL}")
# logger.info(f"Using Firebase credentials: {FIREBASE_CREDENTIALS}")
# logger.info(f"Using Firebase database URL: {FIREBASE_DB_URL}")
# logger.info(f"Using Firebase bucket: {FIREBASE_BUCKET}")
# logger.info(f"Using server name: {SERVER_NAME}")

# Initialize Firebase
cred = credentials.Certificate(FIREBASE_CREDENTIALS)
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred, {
        'databaseURL': FIREBASE_DB_URL,
        'storageBucket': FIREBASE_BUCKET
    })
database = firestore.client().collection("nnServer")  #.document(server_name)
database_server = database.document(SERVER_NAME)

Base = declarative_base()
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

# Create all tables
Base.metadata.create_all(bind=engine)