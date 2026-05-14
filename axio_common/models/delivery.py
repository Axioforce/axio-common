"""
Delivery — terminal node in the mold → load cell → plate → calibrator →
session → run → job → delivery chain.

One row per shipment of a calibrated/trained force plate to a customer.
`device_axf_id` is the plate that left the building; `job_id` is the
training job whose model went out with it (nullable for plates that
ship pre-training, or for backfilled rows whose source isn't known).

Free-form `customer` string for now — promote to a Customer FK in a
follow-up once we have a need to dedupe customers and track contacts.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, String, Text, Index,
)
from sqlalchemy.orm import relationship

from axio_common.database import Base
from axio_common.utils.model_utils import current_time


# Lifecycle states for a Delivery row. Order is meaningful — a delivery
# only moves forward (or to 'returned' which is its own branch).
DELIVERY_STATUS_PENDING = "pending"      # logged, not yet shipped
DELIVERY_STATUS_SHIPPED = "shipped"      # left the building
DELIVERY_STATUS_DELIVERED = "delivered"  # customer received it
DELIVERY_STATUS_RETURNED = "returned"    # came back to us (RMA, etc.)

DELIVERY_STATUSES = (
    DELIVERY_STATUS_PENDING,
    DELIVERY_STATUS_SHIPPED,
    DELIVERY_STATUS_DELIVERED,
    DELIVERY_STATUS_RETURNED,
)


class Delivery(Base):
    """One row per shipment of a plate to a customer."""
    __tablename__ = "deliveries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_axf_id = Column(
        String, ForeignKey("devices.axf_id"),
        nullable=False, index=True,
    )
    # Training job whose model shipped on this plate. Nullable because
    # backfilled rows may predate the M2M tracking, and some plates ship
    # before any training has run.
    job_id = Column(
        String, ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    customer = Column(String, nullable=True, index=True)
    status = Column(
        String, nullable=False, default=DELIVERY_STATUS_PENDING, index=True,
    )

    shipped_at = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    returned_at = Column(DateTime(timezone=True), nullable=True)

    tracking_number = Column(String, nullable=True)
    carrier = Column(String, nullable=True)

    notes = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)
    updated_at = Column(
        DateTime(timezone=True), nullable=False,
        default=current_time, onupdate=current_time,
    )

    device = relationship("Device", lazy="select")
    job = relationship("Job", lazy="select")

    __table_args__ = (
        Index("ix_deliveries_status_shipped_at", "status", "shipped_at"),
        Index("ix_deliveries_device_status", "device_axf_id", "status"),
    )


class DeliveryResponse(BaseModel):
    id: int
    device_axf_id: str
    job_id: Optional[str] = None
    customer: Optional[str] = None
    status: str
    shipped_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    returned_at: Optional[datetime] = None
    tracking_number: Optional[str] = None
    carrier: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DeliveryCreateRequest(BaseModel):
    device_axf_id: str
    job_id: Optional[str] = None
    customer: Optional[str] = None
    status: str = DELIVERY_STATUS_PENDING
    shipped_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    returned_at: Optional[datetime] = None
    tracking_number: Optional[str] = None
    carrier: Optional[str] = None
    notes: Optional[str] = None


class DeliveryUpdateRequest(BaseModel):
    """All fields optional; omitted fields keep their current value."""
    job_id: Optional[str] = None
    customer: Optional[str] = None
    status: Optional[str] = None
    shipped_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    returned_at: Optional[datetime] = None
    tracking_number: Optional[str] = None
    carrier: Optional[str] = None
    notes: Optional[str] = None
