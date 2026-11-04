"""ORM models (technical specification, section 6; phase 2 subset)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Double,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    desc,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from measurehub.core.states import JobStatus
from measurehub.db.base import Base

JsonType = JSON().with_variant(JSONB(), "postgresql")
BigIntPk = BigInteger().with_variant(Integer(), "sqlite")


class InstrumentKind(StrEnum):
    VNA = "vna"


class InstrumentStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    ERROR = "error"


def _enum(enum_cls: type[StrEnum]) -> Enum:
    return Enum(
        enum_cls,
        native_enum=False,
        length=16,
        values_callable=lambda e: [member.value for member in e],
        validate_strings=True,
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=_now)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _timestamp()


class Instrument(Base):
    __tablename__ = "instruments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[InstrumentKind] = mapped_column(_enum(InstrumentKind), default=InstrumentKind.VNA)
    model: Mapped[str | None] = mapped_column(String(100))
    resource: Mapped[str] = mapped_column(String(255))
    driver: Mapped[str] = mapped_column(String(50))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[InstrumentStatus] = mapped_column(
        _enum(InstrumentStatus), default=InstrumentStatus.OFFLINE
    )
    idn: Mapped[str | None] = mapped_column(String(255))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _timestamp()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Method(Base):
    __tablename__ = "methods"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    measurement_type: Mapped[str] = mapped_column(String(50))
    params: Mapped[dict[str, Any]] = mapped_column(JsonType)
    processor: Mapped[str] = mapped_column(String(50))
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _timestamp()


class Sample(Base):
    __tablename__ = "samples"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    material: Mapped[str] = mapped_column(String(100))
    thickness_mm: Mapped[float] = mapped_column(Numeric(10, 4, asdecimal=False))
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _timestamp()


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_instrument_id_status", "instrument_id", "status"),
        Index("ix_jobs_created_at_desc", desc("created_at")),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    method_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("methods.id"))
    instrument_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instruments.id"))
    sample_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("samples.id"))
    params: Mapped[dict[str, Any]] = mapped_column(JsonType)
    status: Mapped[JobStatus] = mapped_column(_enum(JobStatus), default=JobStatus.QUEUED)
    points_total: Mapped[int] = mapped_column(Integer)
    points_done: Mapped[int] = mapped_column(Integer, default=0)
    segments_total: Mapped[int] = mapped_column(Integer)
    segments_done: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    celery_task_id: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _timestamp()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MeasurementPoint(Base):
    __tablename__ = "measurement_points"
    __table_args__ = (UniqueConstraint("job_id", "idx", name="uq_measurement_points_job_id_idx"),)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    idx: Mapped[int] = mapped_column(Integer)
    freq_hz: Mapped[float] = mapped_column(Double)
    s11_re: Mapped[float] = mapped_column(Double)
    s11_im: Mapped[float] = mapped_column(Double)
    s21_re: Mapped[float] = mapped_column(Double)
    s21_im: Mapped[float] = mapped_column(Double)
    measured_at: Mapped[datetime] = _timestamp()


class Result(Base):
    __tablename__ = "results"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), index=True)
    processor: Mapped[str] = mapped_column(String(50))
    processor_version: Mapped[str] = mapped_column(String(20))
    data: Mapped[dict[str, Any]] = mapped_column(JsonType)
    summary: Mapped[dict[str, Any]] = mapped_column(JsonType)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _timestamp()


class JobEvent(Base):
    """Persistent log of a job: status changes, retries, errors (``GET /jobs/{id}/events``)."""

    __tablename__ = "job_events"
    __table_args__ = (Index("ix_job_events_job_id_id", "job_id", "id"),)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    level: Mapped[str] = mapped_column(String(10))
    event: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType)
