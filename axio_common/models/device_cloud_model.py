"""Per-device snapshot of Firebase model-active state.

Upserted by axio-server's cloud-model sync. One row per device. This is an
index of Firebase GCS truth (the `modelActive` blob flag), NOT the source of
truth — it lets the deliveries audit join cloud-active state against live-test
and delivery data without scanning the bucket per request.
"""
from sqlalchemy import Boolean, Column, DateTime, String

from axio_common.database import Base


class DeviceCloudModel(Base):
    """Snapshot of which model is active in Firebase storage for a device."""
    __tablename__ = "device_cloud_models"

    # Canonical dotted axf_id, e.g. "11.00000035".
    device_axf_id = Column(String, primary_key=True)
    # model_id of the active blob, or None when no active model exists.
    cloud_active_model_id = Column(String, nullable=True)
    # True when the device has at least one non-deleted model blob.
    has_cloud_model_files = Column(Boolean, nullable=False, default=False)
    # When this row was last refreshed from Firebase (UTC, tz-aware).
    synced_at = Column(DateTime(timezone=True), nullable=False)
