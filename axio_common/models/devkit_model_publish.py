"""Dev-kit model publishes: one row per attempt to publish a flex's model
image to the cloud model store (Dyno #1277).

A dev kit runs its calibration model out of a signed data image that the
host fetches from the model store (Axiobase `devkitModelPublish` /
`devkitModelLatest`, Cloud SQL `axf.devkit_model`). axio-server builds that
image from the flex's best force job (`app/services/devkit_model_image.py`)
and, since #1277, publishes it by itself when the training finishes
(`app/services/devkit_auto_publish.py`). Every attempt is recorded here,
newest last, whatever its outcome, so the fleet UI can say what is in the
store, what failed and why, and when the next try is due.

The row is also the sweep's memory:

* **Idempotency.** A flex is current in a store when its newest
  `published` row there names the device's best force job and run and the
  pad-stiffness block the pack carried. The store's open read does not say
  which job an image came from, so this table is how that is known.
* **Versions.** The store never goes backwards (deleted rows included), so
  every version this table has seen for a device and store is a floor for
  the next signature.
* **Backoff.** An attempt is written (`started`, `next_attempt_at` already
  pushed out) and committed before the build begins, as `AutoEolAttempt`
  is, so a crash during a build cannot turn into a retry loop.

Keyed by device id (`18.<flex_fp>`, the flex) and store base URL: the dev
and production stores are separate, and each gets its own history.
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel
from sqlalchemy import (
    BigInteger, Column, DateTime, Index, Integer, JSON, String, Text,
)

from axio_common.database import Base
from axio_common.utils.model_utils import current_time

#: Written before the build starts; stays so if the process died during it.
MODEL_PUBLISH_STARTED = "started"
#: The store accepted the image (201), or already held exactly these bytes.
MODEL_PUBLISH_PUBLISHED = "published"
#: A gate refused the model (pointer, artefacts, unit, verify_pack, the
#: plausibility gate, the pad-stiffness header, ...). Nothing was uploaded.
MODEL_PUBLISH_REFUSED = "refused"
#: Built, but the store could not be reached, or refused the upload.
MODEL_PUBLISH_UPLOAD_FAILED = "upload_failed"
#: Anything unexpected.
MODEL_PUBLISH_ERROR = "error"
MODEL_PUBLISH_STATUSES = (
    MODEL_PUBLISH_STARTED, MODEL_PUBLISH_PUBLISHED, MODEL_PUBLISH_REFUSED,
    MODEL_PUBLISH_UPLOAD_FAILED, MODEL_PUBLISH_ERROR,
)

#: Who started the attempt: the server's sweep, or an operator.
MODEL_PUBLISH_SOURCE_AUTO = "auto"
MODEL_PUBLISH_SOURCE_MANUAL = "manual"
MODEL_PUBLISH_SOURCES = (MODEL_PUBLISH_SOURCE_AUTO,
                         MODEL_PUBLISH_SOURCE_MANUAL)


class DevkitModelPublish(Base):
    """One attempt to publish one flex's model image to one store."""

    __tablename__ = "devkit_model_publishes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: `18.<flex_fp>`. Not a ForeignKey, like the rest of the dev-kit tables.
    device_axf_id = Column(String, nullable=False, index=True)
    #: The store's base URL (`https://us-central1-<project>.cloudfunctions.net`).
    store_base = Column(String, nullable=False)
    #: The model: `jobs.id`, the packed YYYYMMDDHHMMSS job UID, and the run.
    job_id = Column(String, nullable=True)
    job_timestamp = Column(BigInteger, nullable=False)
    model_run = Column(Integer, nullable=False)
    #: 'auto' | 'manual'.
    source = Column(String, nullable=False)
    #: 1 + the attempts for this (device, store, job, run) since its last
    #: publish; sets the backoff.
    attempt = Column(Integer, nullable=False, default=1)
    #: See MODEL_PUBLISH_STATUSES.
    status = Column(String, nullable=False)
    #: Why it did not publish (the gate's refusal, the store's answer).
    reason = Column(Text, nullable=True)

    #: The signed image, once built: `1.<YY>.<MDD>+<build>`, its sha256 and
    #: size, and the contract row's model fields.
    version = Column(String, nullable=True)
    image_sha256 = Column(String, nullable=True)
    image_bytes = Column(Integer, nullable=True)
    pack_format = Column(Integer, nullable=True)
    input_unit = Column(String, nullable=True)
    model_artifact = Column(String, nullable=True)
    #: The pad-stiffness block the pack header carries ({e_mpa, k_n_per_mm,
    #: shore_a, tip_mm, unc_pct}); NULL = the all-zero "not measured" block.
    pad_stiffness = Column(JSON, nullable=True)

    #: The store's answer: its row id (`<device_id>/<version>`) and the
    #: account it recorded as the publisher.
    store_model_id = Column(String, nullable=True)
    published_by = Column(String, nullable=True)
    #: HTTP publish requests made (retries on 5xx and timeouts included).
    http_attempts = Column(Integer, nullable=True)
    #: The build's log, last lines only.
    log = Column(JSON, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=False,
                        default=current_time)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    #: The sweep does not try this model again before this time. NULL once
    #: published.
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


Index("ix_devkit_model_publishes_device_store",
      DevkitModelPublish.device_axf_id, DevkitModelPublish.store_base,
      DevkitModelPublish.id)


class DevkitModelPublishResponse(BaseModel):
    """One attempt, as the API returns it."""
    id: int
    device_axf_id: str
    store_base: str
    job_id: Optional[str] = None
    job_timestamp: int
    model_run: int
    source: str
    attempt: int
    status: str
    reason: Optional[str] = None
    version: Optional[str] = None
    image_sha256: Optional[str] = None
    image_bytes: Optional[int] = None
    pack_format: Optional[int] = None
    input_unit: Optional[str] = None
    model_artifact: Optional[str] = None
    pad_stiffness: Optional[dict] = None
    store_model_id: Optional[str] = None
    published_by: Optional[str] = None
    http_attempts: Optional[int] = None
    log: Optional[List[str]] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    next_attempt_at: Optional[datetime] = None

    class Config:
        from_attributes = True
