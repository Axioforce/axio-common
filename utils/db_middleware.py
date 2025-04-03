from sqlalchemy import event
from sqlalchemy.orm import Session

from db_core import database, database_server
from models.run import Run
from models.jobs import Job
from models.client import Client
from models.device import Device
from logger_config import logger

# Firestore collection
devices_doc = database.document("devices")
clients_collection = database_server.collection("clients")
jobs_collection = database_server.collection("jobs")
runs_collection = database_server.collection("runs")
metrics_collection = database_server.collection("metrics")

FIREBASE_SYNC = True  # Disable Firebase sync during startup


def get_firebase_sync_status():
    """
    Get the current status of Firebase sync.
    """
    return FIREBASE_SYNC


def set_firebase_sync_status(status: bool):
    """
    Set the status of Firebase sync.
    """
    global FIREBASE_SYNC
    FIREBASE_SYNC = status


def sync_to_firebase(collection_ref, event_type, target):
    """
    Sync a document to Firebase Firestore with error handling.
    """
    if target.__class__.__name__ == "Client":
        db_id = target.hostname
    else:
        db_id = target.id
    logger.info(f"Syncing {target.__class__.__name__} {db_id} to Firebase.")
    try:
        doc_ref = collection_ref.document(db_id)

        if event_type == "INSERT":
            doc_ref.set(target.to_dict())
            logger.info(f"Inserted {target.__class__.__name__} {db_id} into Firebase.")
        elif event_type == "UPDATE":
            doc_ref.set(target.to_dict(), merge=True)
            logger.info(f"Updated {target.__class__.__name__} {db_id} in Firebase.")
        elif event_type == "DELETE":
            doc_ref.delete()
            logger.info(f"Deleted {target.__class__.__name__} {db_id} from Firebase.")
    except Exception as e:
        logger.error(f"Failed to sync {target.__class__.__name__} {db_id} to Firebase: {e}")


def sync_device_to_firebase(event_type, target):
    """
    Sync a Device instance to Firebase Firestore with error handling.
    """
    logger.info(f"Syncing device {target.axf_id} to Firebase.")
    try:
        device_id = target.axf_id.split(".")[-1]
        doc_ref = devices_doc.collection(target.type_id).document(device_id)

        if event_type == "INSERT":
            doc_ref.set(target.to_dict())
            logger.info(f"Inserted device {target.axf_id} into Firebase.")
        elif event_type == "UPDATE":
            doc_ref.set(target.to_dict(), merge=True)
            logger.info(f"Updated device {target.axf_id} in Firebase.")
        elif event_type == "DELETE":
            doc_ref.delete()
            logger.info(f"Deleted device {target.axf_id} from Firebase.")
    except Exception as e:
        logger.error(f"Failed to sync device {target.axf_id} to Firebase: {e}")


# Attach hooks to the models
@event.listens_for(Device, "after_insert")
def after_insert(mapper, connection, target):
    if FIREBASE_SYNC:
        sync_device_to_firebase("INSERT", target)


@event.listens_for(Device, "after_update")
def after_update(mapper, connection, target):
    if FIREBASE_SYNC:
        sync_device_to_firebase("UPDATE", target)


@event.listens_for(Device, "after_delete")
def after_delete(mapper, connection, target):
    if FIREBASE_SYNC:
        sync_device_to_firebase("DELETE", target)


@event.listens_for(Client, "after_insert")
def after_insert(mapper, connection, target):
    if FIREBASE_SYNC:
        sync_to_firebase(clients_collection, "INSERT", target)


@event.listens_for(Client, "after_update")
def after_update(mapper, connection, target):
    if FIREBASE_SYNC:
        sync_to_firebase(clients_collection, "UPDATE", target)


@event.listens_for(Client, "after_delete")
def after_delete(mapper, connection, target):
    if FIREBASE_SYNC:
        sync_to_firebase(clients_collection, "DELETE", target)


@event.listens_for(Job, "after_insert")
def after_insert(mapper, connection, target):
    if FIREBASE_SYNC:
        sync_to_firebase(jobs_collection, "INSERT", target)


@event.listens_for(Job, "after_update")
def after_update(mapper, connection, target):
    if FIREBASE_SYNC:
        sync_to_firebase(jobs_collection, "UPDATE", target)


@event.listens_for(Job, "after_delete")
def after_delete(mapper, connection, target):
    if FIREBASE_SYNC:
        sync_to_firebase(jobs_collection, "DELETE", target)


@event.listens_for(Run, "after_insert")
def after_insert(mapper, connection, target):
    if FIREBASE_SYNC:
        sync_to_firebase(runs_collection, "INSERT", target)


@event.listens_for(Run, "after_update")
def after_update(mapper, connection, target):
    if FIREBASE_SYNC:
        sync_to_firebase(runs_collection, "UPDATE", target)


@event.listens_for(Run, "after_delete")
def after_delete(mapper, connection, target):
    if FIREBASE_SYNC:
        sync_to_firebase(runs_collection, "DELETE", target)


def sync_all_collections_from_firebase_to_sql(db: Session):
    """
    Sync all collections from Firebase to the SQL database.
    """
    logger.info("Syncing all collections from Firebase to SQL.")
    sync_devices_from_firebase_to_sql(db)
    sync_from_firebase_to_sql(db, clients_collection, Client)
    sync_from_firebase_to_sql(db, jobs_collection, Job)
    sync_from_firebase_to_sql(db, runs_collection, Run)


def sync_jobs_from_firebase_to_sql(db: Session):
    sync_from_firebase_to_sql(db, jobs_collection, Job)


def stream_firestore_in_batches(collection_ref, batch_size=100):
    """
    Generator function to stream Firestore documents in batches.
    """
    docs = collection_ref.limit(batch_size).stream()  # Get initial batch
    last_doc = None  # Track last document for pagination

    while True:
        batch = list(docs)  # Convert generator to list
        if not batch:
            break  # Stop when there are no more documents

        yield batch  # Yield current batch

        last_doc = batch[-1]  # Keep track of last doc in batch
        docs = collection_ref.start_after(last_doc).limit(batch_size).stream()  # Fetch next batch


def sync_from_firebase_to_sql(db: Session, collection_ref, model):
    """
    Sync all documents from a Firestore collection to the SQL database in batches.
    """
    logger.info(f"Starting batch sync for {model.__name__} from Firebase to SQL.")

    total_docs = 0
    for batch in stream_firestore_in_batches(collection_ref, batch_size=100):
        logger.info(f"Processing batch of {len(batch)} {model.__name__} documents from Firebase.")

        for doc in batch:
            data = doc.to_dict()
            instance = db.query(model).filter(model.id == data["id"]).first()

            if not instance:
                # Add new instance to the SQL database
                instance = model.from_dict(data)
                db.add(instance)
                logger.info(f"Added {instance.__class__.__name__} {instance.id} to SQL from Firebase.")
            else:
                # Check if updates are needed
                if (instance.updated_at is None or data.get("updated_at") is None or
                        instance.updated_at < data["updated_at"].replace(tzinfo=None)):
                    instance.update_from_dict(data)
                    logger.info(f"Updated {instance.__class__.__name__} {instance.id} in SQL from Firebase.")

        db.commit()  # Commit after each batch
        total_docs += len(batch)

    logger.info(f"Finished syncing {total_docs} {model.__name__} documents from Firebase to SQL.")


def sync_devices_from_firebase_to_sql(db: Session):
    """
    Sync all devices from Firebase to the SQL database only if they are missing or outdated.
    """
    logger.info("Syncing devices from Firebase to SQL.")
    device_types = list(devices_doc.collections())
    logger.info(f"Found {len(device_types)} device types in Firebase.")

    for device_type_collection in device_types:
        devices = device_type_collection.stream()

        for doc in devices:
            data = doc.to_dict()
            device = db.query(Device).filter(Device.axf_id == data["axf_id"]).first()

            if not device:
                # Add new device to the SQL database
                device = Device.from_dict(data)
                db.add(device)
                logger.info(f"Added device {device.axf_id} to SQL from Firebase.")
            else:
                # Check if updates are needed
                if (device.updated_at is None or data.get("updated_at") is None or
                        device.updated_at < data["updated_at"].replace(tzinfo=None)):
                    device.from_dict(data)
                logger.info(f"Updated device {device.axf_id} in SQL from Firebase.")
    db.commit()


if __name__ == "__main__":
    from database import Base, engine, SessionLocal
    from logger_config import logger

    logger.info("Initializing database...")
    Base.metadata.create_all(bind=engine)
    logger.info("Database initialized with the following tables:")
    for table in Base.metadata.tables:
        logger.info(f"  {table}")
    # Create a session and sync Firebase to SQL **only once at startup**
    db = SessionLocal()
    logger.info("Syncing Firebase data to SQL on startup...")
    sync_all_collections_from_firebase_to_sql(db)
    db.close()