"""Dev-kit model releases: the ledger of every flex model axio-server decided
to release to production (Dyno #1421).

A finished training job reaches the dev model store by itself (#1277). From
there to an installed production model there were three hand steps: copy the
open-key image to production, seal it into a closed-key store entry, and
decide whether the entry goes active or draft. axio-server now makes the
decision and keeps the record here; the AxioSeal job (Cloud Run, project
`axioforce-seal`) pulls the pending rows, does the two key-touching steps and
reports each outcome back onto the row.

One row per (flex, dev version): `unique (device_axf_id, version)`. Keyed by
the dev-kit device id `18.<flex_fp>`; not a ForeignKey, like the rest of the
dev-kit tables.

**Verdict.** `active` or `draft`, with `reasons` (a list of short strings:
why it is draft, or why it is active) and `evidence` (what was looked at: the
EOL row ids, the current recipe, the production entry it replaces). The
gates are axio-server's (`app/services/devkit_release.py`); this module only
stores the answer.

**State.** `pending` (decided, nothing done) -> `copied` (the open image is on
production) -> `done` (sealed; `sealed_status` is the status the entry was
sealed with, `active` or `draft`). A failed step keeps the state, records
`last_error` and counts `attempts`; after `MAX_RELEASE_ATTEMPTS` failures the
row is `failed` and is no longer served. `sealed` is reserved for a sealed
entry whose status is not yet confirmed; nothing writes it today.
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel
from sqlalchemy import (
    BigInteger, Column, DateTime, Integer, JSON, String, Text,
    UniqueConstraint,
)

from axio_common.database import Base
from axio_common.utils.model_utils import current_time

RELEASE_VERDICT_ACTIVE = "active"
RELEASE_VERDICT_DRAFT = "draft"
RELEASE_VERDICTS = (RELEASE_VERDICT_ACTIVE, RELEASE_VERDICT_DRAFT)

#: Decided; nothing done on production yet.
RELEASE_STATE_PENDING = "pending"
#: The open-key image is on production (copy-open done or already there).
RELEASE_STATE_COPIED = "copied"
#: Reserved: sealed, status not confirmed.
RELEASE_STATE_SEALED = "sealed"
#: Sealed into a production store entry; `sealed_status` says how.
RELEASE_STATE_DONE = "done"
#: Gave up after MAX_RELEASE_ATTEMPTS failed steps.
RELEASE_STATE_FAILED = "failed"
RELEASE_STATES = (RELEASE_STATE_PENDING, RELEASE_STATE_COPIED,
                  RELEASE_STATE_SEALED, RELEASE_STATE_DONE,
                  RELEASE_STATE_FAILED)
#: The states the AxioSeal job is still owed work on.
RELEASE_OPEN_STATES = (RELEASE_STATE_PENDING, RELEASE_STATE_COPIED,
                       RELEASE_STATE_SEALED)

RELEASE_STEP_COPY_OPEN = "copy_open"
RELEASE_STEP_SEAL_ENTRY = "seal_entry"
RELEASE_STEPS = (RELEASE_STEP_COPY_OPEN, RELEASE_STEP_SEAL_ENTRY)

RELEASE_OUTCOME_DONE = "done"
RELEASE_OUTCOME_ALREADY = "already"
RELEASE_OUTCOME_FAILED = "failed"
RELEASE_OUTCOMES = (RELEASE_OUTCOME_DONE, RELEASE_OUTCOME_ALREADY,
                    RELEASE_OUTCOME_FAILED)

#: Failed steps before a release is `failed` and no longer served.
MAX_RELEASE_ATTEMPTS = 5


class DevkitRelease(Base):
    """One flex model version on its way from the dev store to production."""

    __tablename__ = "devkit_releases"
    __table_args__ = (
        UniqueConstraint("device_axf_id", "version",
                         name="uq_devkit_releases_device_version"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_axf_id = Column(String, nullable=False, index=True)
    flex_fp = Column(String, nullable=False)
    #: The dev store's version (`1.YY.MDD+build`) being released.
    version = Column(String, nullable=False)
    #: The dev store row it copies (`18.<fp>/<version>`).
    dev_row_id = Column(String, nullable=False)
    #: The dev image's sha256, as the dev store reported it.
    dev_image_sha256 = Column(String, nullable=True)
    #: The training job behind the dev image (its integer UID and run), from
    #: the automatic publisher's record; NULL = not known.
    job_id = Column(String, nullable=True)
    job_timestamp = Column(BigInteger, nullable=True)
    model_run = Column(Integer, nullable=True)
    #: The job's DEVKIT_RECIPE; NULL = none / not known.
    recipe = Column(String, nullable=True)

    #: RELEASE_VERDICTS.
    verdict = Column(String, nullable=False)
    #: Short strings: why draft, or why active.
    reasons = Column(JSON, nullable=False, default=list)
    #: What the verdict was formed from (EOL row ids, current recipe, the
    #: production entry it replaces, ...).
    evidence = Column(JSON, nullable=True)
    #: When the verdict was last (re)formed.
    evaluated_at = Column(DateTime(timezone=True), nullable=True)

    #: RELEASE_STATES.
    state = Column(String, nullable=False, default=RELEASE_STATE_PENDING,
                   index=True)
    #: The production open-key row id copy-open reported.
    production_row_id = Column(String, nullable=True)
    #: The sealed store entry's sha256, from seal-entry.
    entry_sha256 = Column(String, nullable=True)
    #: The status the entry was sealed with (`active` | `draft`).
    sealed_status = Column(String, nullable=True)
    last_step = Column(String, nullable=True)
    last_detail = Column(Text, nullable=True)
    last_error = Column(Text, nullable=True)
    #: Failed steps so far.
    attempts = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=current_time)
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=current_time, onupdate=current_time)

    def to_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}

    def apply_report(self, step: str, outcome: str, *,
                     detail: Optional[str] = None,
                     production_row_id: Optional[str] = None,
                     entry_sha256: Optional[str] = None,
                     status: Optional[str] = None) -> bool:
        """Record one step's outcome, as the AxioSeal job reported it.

        Returns True when the row changed. Raises `ValueError` for an
        unknown step / outcome / status, and `ReleaseReportRefused` for a
        report the row's state cannot take (a failed release; a failure
        reported on a finished one). Does not commit.

        - `copy_open` done / already: `pending` -> `copied`
          (`production_row_id` recorded).
        - `seal_entry` done / already: -> `done`, `sealed_status` = `status`
          (`entry_sha256` recorded). Accepted from any open state: the
          store is the truth, and it says the entry exists.
        - `failed`: `attempts` + 1, `last_error` = `detail`; at
          MAX_RELEASE_ATTEMPTS the row is `failed`.
        - A success reported again on a finished release is a no-op.
        """
        if step not in RELEASE_STEPS:
            raise ValueError(f"step must be one of {RELEASE_STEPS}")
        if outcome not in RELEASE_OUTCOMES:
            raise ValueError(f"outcome must be one of {RELEASE_OUTCOMES}")
        if status is not None and status not in RELEASE_VERDICTS:
            raise ValueError(f"status must be one of {RELEASE_VERDICTS} or null")
        if self.state == RELEASE_STATE_FAILED:
            raise ReleaseReportRefused(
                f"release {self.id} is failed after {self.attempts} failed "
                f"steps; it takes no more reports")
        if self.state == RELEASE_STATE_DONE:
            if outcome == RELEASE_OUTCOME_FAILED:
                raise ReleaseReportRefused(
                    f"release {self.id} is done; a failure cannot be "
                    f"recorded on it")
            return False

        self.last_step = step
        self.last_detail = detail
        if outcome == RELEASE_OUTCOME_FAILED:
            self.attempts = (self.attempts or 0) + 1
            self.last_error = detail or f"{step} failed"
            if self.attempts >= MAX_RELEASE_ATTEMPTS:
                self.state = RELEASE_STATE_FAILED
            return True

        self.last_error = None
        if step == RELEASE_STEP_COPY_OPEN:
            if production_row_id:
                self.production_row_id = production_row_id
            if self.state == RELEASE_STATE_PENDING:
                self.state = RELEASE_STATE_COPIED
            return True
        if entry_sha256:
            self.entry_sha256 = entry_sha256
        if production_row_id and not self.production_row_id:
            self.production_row_id = production_row_id
        self.sealed_status = status
        self.state = RELEASE_STATE_DONE
        return True


class ReleaseReportRefused(Exception):
    """A step report the release's state cannot take (HTTP 409)."""


class DevkitReleaseResponse(BaseModel):
    """One release, as the API returns it."""
    id: int
    device_axf_id: str
    flex_fp: str
    version: str
    dev_row_id: str
    dev_image_sha256: Optional[str] = None
    job_id: Optional[str] = None
    job_timestamp: Optional[int] = None
    model_run: Optional[int] = None
    recipe: Optional[str] = None
    verdict: str
    reasons: List[str] = []
    evidence: Optional[dict] = None
    evaluated_at: Optional[datetime] = None
    state: str
    production_row_id: Optional[str] = None
    entry_sha256: Optional[str] = None
    sealed_status: Optional[str] = None
    last_step: Optional[str] = None
    last_detail: Optional[str] = None
    last_error: Optional[str] = None
    attempts: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
