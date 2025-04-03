from typing import Optional
from fastapi import Request
from sqlalchemy.orm import Session

from axio_common.models import Device, Job, Client
from axio_common.logger import logger, hostname_filter


MAX_TIME_DIFF = 600  # Maximum time difference in seconds between current time and last heartbeat
MAX_INACTIVE_TIME = 86400  # Maximum time in seconds for a client to be considered inactive


def register_client(client_ip: str, hostname: Optional[str], db: Session, daemon: Optional[bool] = True) -> Client | None:
    """
    Register a new client in the database.
    """
    if hostname is None:
        logger.info(f"Client hostname not provided for {client_ip}")
        return None
    new_client = Client(
        hostname=hostname,
        ip_address=client_ip,
        daemon=daemon
    )
    db.add(new_client)
    db.commit()
    db.refresh(new_client)
    logger.info(f"New client registered: {client_ip} -> {new_client.hostname}")
    return new_client


def check_client_hostname(client, hostname: Optional[str], db: Session):
    if client and hostname and hostname != client.hostname and hostname != f'{client.ip_address}':
        client.update_hostname(hostname, db)
        logger.info(f"Client hostname updated: {client.hostname} -> {hostname}")
    elif not client:
        logger.warning(f"Client {client.hostname} not found.")


def client_by_hostname(hostname: str, db: Session, update_activity=True) -> Optional[Client]:
    """
    Fetch a client by its hostname from the database.
    """
    client = db.query(Client).filter(Client.hostname == hostname).first()
    if client:
        if update_activity:
            client.mark_active(db)
    return client


def client_by_id(client_id: str, db: Session) -> Optional[Client]:
    """
    Fetch a client by its ID from the database.
    """
    client = db.query(Client).filter(Client.id == client_id).first()
    if client:
        client.mark_active(db)
    return client


def device_by_id(device_id: str, db: Session):
    return db.query(Device).filter(Device.axf_id == device_id).first()


# Function to resolve the hostname for a given client IP address
def resolve_hostname(client_request: Request, db: Session, hostname: Optional[str] = None):
    """
        Resolve the hostname for a client. Register the client if not already registered.
    """
    # Extract client IP
    client_ip = client_request.headers.get("X-Forwarded-For", client_request.client.host).split(",")[0].strip()

    # Lookup client in the database
    if hostname:
        client = client_by_hostname(hostname, db)
    else:
        logger.warning(f'Client hostname not provided for {client_ip}')
        return None
    if not client:
        client = register_client(client_ip, hostname, db)

    hostname_filter.set_hostname(client.hostname)
    hostname_filter.set_ipaddress(client_ip)
    if client_ip != client.ip_address:
        client.update_ip(client_ip, db)
    return client


# Function to resolve the device for a given device axf id
def resolve_device(job_config, db: Session):
    device_id = job_config.get("DEVICE_ID").replace("-", ".")
    device = device_by_id(device_id, db)
    if not device:
        logger.info(f"Device {device_id} not found.")
        device = Device(device_id)
        db.add(device)
        db.commit()
        db.refresh(device)

    return device


def get_job_by_id(job_id: str, db: Session):
    logger.info(f"Searching for job {job_id}")
    job = db.query(Job).filter(Job.id == job_id).first()
    if job:
        logger.info(f"Job {job_id} found.")
    else:
        logger.error(f"Job {job_id} not found.")
    return job


def update_job_status(job, status: str, db: Session, hostname=None):
    """
        Update the status of a job in the database.
        """
    if isinstance(job, str):
        job = get_job_by_id(job, db)
    if not job:
        raise ValueError(f"Job with ID {job} not found")

    # Update the job's status
    job.update_status(status, db, hostname)

    logger.info(f"Job {job.id} status updated to {status}")
    return job
