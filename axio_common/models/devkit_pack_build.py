"""Dev-kit model pack builds: one row per pack axio-server built for an
operator to put in an open base's model store (Dyno #1416).

The internal DAQ can run any server model on the attached flex: an operator
picks a source flex's job and run, axio-server builds its pack bound to the
TARGET flex (the attached one) and hands the plaintext pack back; the DAQ adds
it to the base's model store. When the source and target differ the kit runs
a model that was not trained on its pad, which gives plausible but wrong
forces, so every build is recorded here: who asked, which model, for which
flex, when, and the exact bytes (sha256) that left the server.

Nothing is published: the pack goes back in the response only. Keyed by the
TARGET device id (`18.<flex_fp>`, the flex the pack is bound to); not a
ForeignKey, like the rest of the dev-kit tables.
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Index, Integer, JSON, String, Text,
)

from axio_common.database import Base
from axio_common.utils.model_utils import current_time

#: Written before the build starts; stays so if the process died during it.
PACK_BUILD_STARTED = "started"
#: Every gate passed; the pack was returned to the caller.
PACK_BUILD_BUILT = "built"
#: A gate refused the model (pointer, artifacts, unit, reference vectors,
#: verify_pack, the plausibility gate, the rebinding check). Nothing returned.
PACK_BUILD_REFUSED = "refused"
#: Anything unexpected.
PACK_BUILD_ERROR = "error"
PACK_BUILD_STATUSES = (PACK_BUILD_STARTED, PACK_BUILD_BUILT,
                       PACK_BUILD_REFUSED, PACK_BUILD_ERROR)


class DevkitPackBuild(Base):
    """One pack build: a source flex's model bound to a target flex."""

    __tablename__ = "devkit_pack_builds"

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: The caller: the token's role, and the name the client sent
    #: (`operator (stephen@...)`), as the best-model pin records it.
    requested_by = Column(String, nullable=False)
    #: The model: the flex it was trained on, its job and run.
    source_device_axf_id = Column(String, nullable=False, index=True)
    job_id = Column(String, nullable=True)
    job_timestamp = Column(BigInteger, nullable=False)
    model_run = Column(Integer, nullable=False)
    #: The job's DEVKIT_RECIPE, or NULL for a grid-search job.
    recipe = Column(String, nullable=True)
    #: The flex the pack is bound to.
    target_device_axf_id = Column(String, nullable=False)
    #: True when the target is not the source: a model on another pad.
    is_foreign = Column(Boolean, nullable=False)
    #: See PACK_BUILD_STATUSES.
    status = Column(String, nullable=False)
    #: Why it was not built (the gate's refusal).
    reason = Column(Text, nullable=True)

    #: The pack that was returned: sha256, size, format, unit, the entries'
    #: names (a foreign build's are `from-18.<source>`), and the pad block
    #: its header carries (the TARGET flex's; NULL = all zero).
    pack_sha256 = Column(String, nullable=True)
    pack_bytes = Column(Integer, nullable=True)
    pack_format = Column(Integer, nullable=True)
    input_unit = Column(String, nullable=True)
    entry_names = Column(JSON, nullable=True)
    pad_stiffness = Column(JSON, nullable=True)
    #: The build's log, last lines only.
    log = Column(JSON, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=False,
                        default=current_time)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


Index("ix_devkit_pack_builds_target", DevkitPackBuild.target_device_axf_id,
      DevkitPackBuild.id)


class DevkitPackBuildResponse(BaseModel):
    """One build, as the API returns it."""
    id: int
    requested_by: str
    source_device_axf_id: str
    job_id: Optional[str] = None
    job_timestamp: int
    model_run: int
    recipe: Optional[str] = None
    target_device_axf_id: str
    is_foreign: bool
    status: str
    reason: Optional[str] = None
    pack_sha256: Optional[str] = None
    pack_bytes: Optional[int] = None
    pack_format: Optional[int] = None
    input_unit: Optional[str] = None
    entry_names: Optional[List[str]] = None
    pad_stiffness: Optional[dict] = None
    log: Optional[List[str]] = None
    started_at: datetime
    finished_at: Optional[datetime] = None

    class Config:
        from_attributes = True
