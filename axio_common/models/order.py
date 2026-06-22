"""
Order — a unit to be shipped, the entry point of the fulfillment flow.

Mirrors a row of the production team's `Order Tracker V2.xlsx` (QuickBooks-fed,
maintained on OneDrive). For now orders are *mirrored* into Postgres by a sync
run on the shipping PC (which can see OneDrive); the intent is to make Postgres
the source of truth and retire the spreadsheet later, so `source` /
`source_row_num` track provenance and let the sync upsert idempotently.

One order can ship multiple devices (the tracker's Part1..Part5 serials, which
are device axf_ids). Shipping an order creates one `Delivery` row per device,
all sharing the shipment's tracking number — see Delivery.order_id.

This row holds the SHIPMENT-level facts (customer, address, tracking, cost,
generated-PDF storage keys); per-device state lives on Delivery.
"""
from datetime import date, datetime
from typing import Any, List, Optional

from pydantic import BaseModel
from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, Integer, String, Text, Index,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from axio_common.database import Base
from axio_common.utils.model_utils import current_time


# Lifecycle of an order row.
ORDER_STATUS_OPEN = "open"            # awaiting fulfillment
ORDER_STATUS_SHIPPED = "shipped"      # label created, devices left the building
ORDER_STATUS_CANCELLED = "cancelled"  # will not ship

ORDER_STATUSES = (
    ORDER_STATUS_OPEN,
    ORDER_STATUS_SHIPPED,
    ORDER_STATUS_CANCELLED,
)

# Provenance of the row.
ORDER_SOURCE_TRACKER = "tracker"  # mirrored from Order Tracker V2.xlsx
ORDER_SOURCE_MANUAL = "manual"    # entered directly in the dashboard


class Order(Base):
    """One unit-to-ship, mirrored from the tracker or entered manually."""
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # --- provenance / idempotent sync ---
    source = Column(String, nullable=False, default=ORDER_SOURCE_TRACKER, index=True)
    # 1-based row number in Order Tracker V2.xlsx for tracker-sourced rows; the
    # sync upserts on (source, source_row_num). NULL for manual orders.
    source_row_num = Column(Integer, nullable=True, index=True)
    synced_at = Column(DateTime(timezone=True), nullable=True)

    # --- order identity / status ---
    qb_ref = Column(String, nullable=True, index=True)   # QuickBooks reference
    status = Column(String, nullable=False, default=ORDER_STATUS_OPEN, index=True)
    pay_status = Column(String, nullable=True)
    is_sample = Column(Boolean, nullable=False, default=False)

    # --- product / customer ---
    items = Column(String, nullable=True)        # tracker "Items on Order", maps to catalog
    customer = Column(String, nullable=True, index=True)
    contact = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    email = Column(String, nullable=True)
    address = Column(Text, nullable=True)        # raw address string; parsed at ship time
    # Device axf_ids on this order (tracker Part1..Part5). list[str].
    serials = Column(JSONB, nullable=True)

    # --- shipment outcome (filled when shipped) ---
    ship_date = Column(Date, nullable=True)
    tracking_number = Column(String, nullable=True)
    shipping_cost = Column(Float, nullable=True)
    # Tigris object keys for the generated, FedEx-account-linked documents.
    # Served only through short-lived authenticated links (never public).
    label_keys = Column(JSONB, nullable=True)            # list[str]
    invoice_key = Column(String, nullable=True)
    packing_list_key = Column(String, nullable=True)

    notes = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=current_time)
    updated_at = Column(
        DateTime(timezone=True), nullable=False,
        default=current_time, onupdate=current_time,
    )

    deliveries = relationship("Delivery", back_populates="order", lazy="select")

    __table_args__ = (
        Index("ix_orders_status_qb_ref", "status", "qb_ref"),
        Index("ix_orders_source_row", "source", "source_row_num"),
    )


class OrderResponse(BaseModel):
    id: int
    source: str
    source_row_num: Optional[int] = None
    synced_at: Optional[datetime] = None
    qb_ref: Optional[str] = None
    status: str
    pay_status: Optional[str] = None
    is_sample: bool
    items: Optional[str] = None
    customer: Optional[str] = None
    contact: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    serials: Optional[List[str]] = None
    ship_date: Optional[date] = None
    tracking_number: Optional[str] = None
    shipping_cost: Optional[float] = None
    label_keys: Optional[List[str]] = None
    invoice_key: Optional[str] = None
    packing_list_key: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class OrderSyncRow(BaseModel):
    """One open-order row pushed up by the shipping-PC tracker sync."""
    source_row_num: int
    qb_ref: Optional[str] = None
    pay_status: Optional[str] = None
    is_sample: bool = False
    items: Optional[str] = None
    customer: Optional[str] = None
    contact: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    serials: Optional[List[str]] = None


class OrderSyncRequest(BaseModel):
    """Full set of open orders from the tracker. The server reconciles:
    upsert each row by source_row_num; tracker rows no longer present (and
    still 'open') are marked closed/stale per the server's policy."""
    rows: List[OrderSyncRow]
