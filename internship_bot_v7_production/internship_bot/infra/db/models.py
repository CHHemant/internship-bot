"""
Database Models — SQLAlchemy 2.0 async ORM.

Tables:
  application_records  — full per-application state
  analytics_snapshots  — periodic analytics output (for charting)
  audit_events         — DB-level audit log (mirrors .vault/audit.log)
  preference_snapshots — versioned UserPrefs history

All sensitive text columns (resume_path, cover_letter_path) store
encrypted paths — actual file content lives encrypted on disk.
"""

from __future__ import annotations
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, Integer, String, Text,
    func, Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ApplicationRecordORM(Base):
    __tablename__ = "application_records"

    id:                  Mapped[str]           = mapped_column(String(64), primary_key=True)
    listing_id:          Mapped[str]           = mapped_column(String(64), index=True)
    listing_title:       Mapped[str]           = mapped_column(String(256))
    listing_company:     Mapped[str]           = mapped_column(String(256))
    listing_country:     Mapped[str]           = mapped_column(String(32))
    listing_portal:      Mapped[str]           = mapped_column(String(64))
    listing_url:         Mapped[str]           = mapped_column(Text)
    listing_deadline:    Mapped[datetime|None] = mapped_column(DateTime, nullable=True)

    # Encrypted file paths (actual files are AES-encrypted on disk)
    resume_path:         Mapped[str]           = mapped_column(Text, default="")
    cover_letter_path:   Mapped[str]           = mapped_column(Text, default="")

    # Verification scores
    ats_score:           Mapped[float]         = mapped_column(Float, default=0.0)
    quality_score:       Mapped[float]         = mapped_column(Float, default=0.0)
    required_coverage:   Mapped[float]         = mapped_column(Float, default=0.0)
    verification_passed: Mapped[bool]          = mapped_column(Boolean, default=False)
    retry_count:         Mapped[int]           = mapped_column(Integer, default=0)

    # Application lifecycle
    status:              Mapped[str]           = mapped_column(String(32), default="queued", index=True)
    confirmation_id:     Mapped[str|None]      = mapped_column(String(128), nullable=True)
    submitted_at:        Mapped[datetime|None] = mapped_column(DateTime, nullable=True)
    response_received_at: Mapped[datetime|None] = mapped_column(DateTime, nullable=True)
    rejection_reason:    Mapped[str|None]      = mapped_column(Text, nullable=True)
    error_log:           Mapped[list]          = mapped_column(JSON, default=list)

    # Timestamps
    created_at:          Mapped[datetime]      = mapped_column(DateTime, default=func.now())
    last_updated:        Mapped[datetime]      = mapped_column(DateTime, default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_app_country_status", "listing_country", "status"),
        Index("ix_app_portal_status", "listing_portal", "status"),
    )


class AnalyticsSnapshotORM(Base):
    __tablename__ = "analytics_snapshots"

    id:               Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    computed_at:      Mapped[datetime] = mapped_column(DateTime, default=func.now())
    total_apps:       Mapped[int]      = mapped_column(Integer)
    total_responded:  Mapped[int]      = mapped_column(Integer)
    response_rate:    Mapped[float]    = mapped_column(Float)
    country_rates:    Mapped[dict]     = mapped_column(JSON)    # {country: rate}
    domain_rates:     Mapped[dict]     = mapped_column(JSON)
    ats_correlation:  Mapped[dict]     = mapped_column(JSON)
    updated_weights:  Mapped[dict]     = mapped_column(JSON)    # new pref weights


class AuditEventORM(Base):
    __tablename__ = "audit_events"

    id:         Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts:         Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True)
    event:      Mapped[str]      = mapped_column(String(128))
    details:    Mapped[dict]     = mapped_column(JSON, default=dict)
    # Note: PII scrubbed before insert — see security.vault.PiiScrubber


class PreferenceSnapshotORM(Base):
    __tablename__ = "preference_snapshots"

    id:           Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_at:  Mapped[datetime] = mapped_column(DateTime, default=func.now())
    prefs_json:   Mapped[dict]     = mapped_column(JSON)    # full UserPrefs dict
    trigger:      Mapped[str]      = mapped_column(String(64), default="analytics_cycle")
