"""
Load cell catalog + per-cell manufacturing metadata + per-cell and per-mold notes.

A load cell ID like "MC8.143" or "1.2.11b.98" is split at the LAST dot into
mold_id (everything before) and batch_id (everything after). Manufacturing
metadata comes from the Device Master List backfill: glue dates, magnet
material, peg offsets, and lifecycle xyz baselines (before/after glue) that
the desktop didn't previously persist.

Per-mold notes are for tracking mag placement, tolerance, and other
per-mold differences that affect cells made from a given mold.
"""
from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel
from sqlalchemy import (
    Column, String, Text, DateTime, Integer, ForeignKey, Index,
)
from sqlalchemy.orm import relationship

from axio_common.database import Base
from axio_common.utils.model_utils import current_time


def parse_load_cell_id(load_cell_id):
    """Split load_cell_id at the last dot. Returns (mold_id, batch_id) or (None, None)."""
    if not load_cell_id or not isinstance(load_cell_id, str):
        return None, None
    s = load_cell_id.strip()
    if '.' not in s:
        return None, None
    mold, batch = s.rsplit('.', 1)
    if not mold or not batch:
        return None, None
    return mold, batch


class LoadCell(Base):
    __tablename__ = "load_cells"

    load_cell_id = Column(String, primary_key=True)
    mold_id = Column(String, nullable=True, index=True)
    batch_id = Column(String, nullable=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=False, default=current_time)

    notes = relationship(
        "LoadCellNote", back_populates="load_cell",
        cascade="all, delete-orphan", lazy="select",
    )
    manufacturing = relationship(
        "LoadCellManufacturing", back_populates="load_cell",
        uselist=False, cascade="all, delete-orphan", lazy="select",
    )

    def to_dict(self):
        return {
            "load_cell_id": self.load_cell_id,
            "mold_id": self.mold_id,
            "batch_id": self.batch_id,
            "first_seen_at": self.first_seen_at,
        }


class LoadCellManufacturing(Base):
    __tablename__ = "load_cell_manufacturing"

    load_cell_id = Column(String, ForeignKey("load_cells.load_cell_id"), primary_key=True)
    date_glued = Column(DateTime(timezone=True), nullable=True)
    mag_material = Column(String, nullable=True)
    outer_mag_peg_offset = Column(String, nullable=True)
    inner_mag_peg_offset = Column(String, nullable=True)
    # Lifecycle baselines stored as "x,y,z" CSV — they're rare-read context
    # data, not query targets. Keeps the schema simple vs a separate child table.
    outer_xyz_before_glue = Column(String, nullable=True)
    outer_xyz_after_glue = Column(String, nullable=True)
    inner_xyz_before_glue = Column(String, nullable=True)
    inner_xyz_after_glue = Column(String, nullable=True)
    source = Column(String, nullable=True)  # provenance, e.g. 'master-list-backfill'

    load_cell = relationship("LoadCell", back_populates="manufacturing")

    def to_dict(self):
        return {
            "load_cell_id": self.load_cell_id,
            "date_glued": self.date_glued,
            "mag_material": self.mag_material,
            "outer_mag_peg_offset": self.outer_mag_peg_offset,
            "inner_mag_peg_offset": self.inner_mag_peg_offset,
            "outer_xyz_before_glue": self.outer_xyz_before_glue,
            "outer_xyz_after_glue": self.outer_xyz_after_glue,
            "inner_xyz_before_glue": self.inner_xyz_before_glue,
            "inner_xyz_after_glue": self.inner_xyz_after_glue,
            "source": self.source,
        }


class LoadCellNote(Base):
    __tablename__ = "load_cell_notes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    load_cell_id = Column(String, ForeignKey("load_cells.load_cell_id"), nullable=False, index=True)
    note = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)
    # 'manual' = operator-typed; 'swap-out' = auto-generated when this cell was
    # removed from a corner with a reason; 'backfill' = imported from spreadsheet.
    context = Column(String, nullable=True)

    load_cell = relationship("LoadCell", back_populates="notes")

    def to_dict(self):
        return {
            "id": self.id,
            "load_cell_id": self.load_cell_id,
            "note": self.note,
            "created_at": self.created_at,
            "context": self.context,
        }


class MoldNote(Base):
    __tablename__ = "mold_notes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mold_id = Column(String, nullable=False, index=True)
    note = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)

    def to_dict(self):
        return {
            "id": self.id,
            "mold_id": self.mold_id,
            "note": self.note,
            "created_at": self.created_at,
        }


# ----- Pydantic request/response shapes -----


class LoadCellResponse(BaseModel):
    load_cell_id: str
    mold_id: Optional[str]
    batch_id: Optional[str]
    first_seen_at: datetime

    class Config:
        from_attributes = True


class LoadCellManufacturingRequest(BaseModel):
    date_glued: Optional[datetime] = None
    mag_material: Optional[str] = None
    outer_mag_peg_offset: Optional[str] = None
    inner_mag_peg_offset: Optional[str] = None
    outer_xyz_before_glue: Optional[str] = None
    outer_xyz_after_glue: Optional[str] = None
    inner_xyz_before_glue: Optional[str] = None
    inner_xyz_after_glue: Optional[str] = None
    source: Optional[str] = None


class NoteRequest(BaseModel):
    note: str
    context: Optional[str] = None
