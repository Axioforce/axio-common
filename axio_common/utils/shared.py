from datetime import timedelta
from typing import Optional
from sqlalchemy.orm import Session

from axio_common.models import Device, Job, Client
from axio_common.logger import logger, hostname_filter
from axio_common.utils.model_utils import current_time


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
def resolve_hostname(client_request, db: Session, hostname: Optional[str] = None):
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
        device_name = job_config.get("DEVICE_NAME", None)
        logger.info(f"Device {device_id} not found.")
        device = Device(device_id, device_name)
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


def update_job_status(job, status: str, db: Session, hostname=None, reason=None):
    """
        Update the status of a job in the database.
        """
    if isinstance(job, str):
        job = get_job_by_id(job, db)
    if not job:
        raise ValueError(f"Job with ID {job} not found")

    # Update the job's status
    job.update_status(status, db, hostname, reason=reason)

    logger.info(f"Job {job.id} status updated to {status}")
    return job


def requeue_missed_heartbeats(db: Session) -> int:
    """Requeue every `running`/`assigned` job whose last heartbeat is older than
    MAX_TIME_DIFF, with `requeue_cause = "heartbeat"` so the worker that held
    it may reclaim it (job leases, #1585). The only place a missed heartbeat
    requeues a job: axio-server's monitor runs it every 60 s and
    `POST /management/check_heartbeats/` on demand. Returns how many it requeued.

    Safe to run concurrently (one per Fly machine): the rows are locked with
    SKIP LOCKED and the staleness test is in the WHERE clause, so Postgres
    re-checks it against a heartbeat that committed meanwhile, and nothing is
    committed until every row is done (a mid-loop commit would release the
    locks on the rows not yet handled)."""
    cutoff = current_time() - timedelta(seconds=MAX_TIME_DIFF)
    jobs = (
        db.query(Job)
        .filter(Job.status.in_(["running", "assigned"]), Job.last_heartbeat < cutoff)
        .with_for_update(skip_locked=True)
        .all()
    )
    for job in jobs:
        logger.warning(f"Job {job.id} missed heartbeat")
        # Only a 'running' job holds an active-job slot (counted on its first
        # progress report). Adjusted inline: Client's helpers commit.
        if job.status == "running" and job.hostname:
            client = db.query(Client).filter(Client.hostname == job.hostname).first()
            if client:
                client.active_jobs = max(0, (client.active_jobs or 0) - 1)
        job.status = "queued"
        job.requeue_cause = "heartbeat"
        job.stopped_at = current_time()
        job.failure_count = (job.failure_count or 0) + 1
        # Record WHY so the dashboard can explain this silent requeue: the
        # worker never reported a reason; it went unresponsive.
        job.failure_reason = (
            f"Requeued: missed heartbeat — worker "
            f"'{job.hostname or '?'}' went unresponsive "
            f"(no heartbeat for >{int(MAX_TIME_DIFF)}s). Likely a "
            f"hard kill (OOM/SIGKILL), machine reboot, or network "
            f"drop; no Python traceback was reported."
        )
        logger.info(f"Job {job.id} moved back to queue")
    db.commit()
    return len(jobs)
