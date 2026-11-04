"""Request and response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from measurehub.core.states import JobStatus
from measurehub.db.models import InstrumentKind, InstrumentStatus


class Page[T](BaseModel):
    items: list[T]
    total: int


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# -- auth -------------------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(_Out):
    id: uuid.UUID
    username: str
    is_active: bool


# -- instruments ------------------------------------------------------------------------------


class InstrumentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: InstrumentKind = InstrumentKind.VNA
    model: str | None = Field(default=None, max_length=100)
    resource: str = Field(min_length=1, max_length=255)
    driver: str = Field(min_length=1, max_length=50)
    is_enabled: bool = True


class InstrumentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    model: str | None = Field(default=None, max_length=100)
    resource: str | None = Field(default=None, min_length=1, max_length=255)
    driver: str | None = Field(default=None, min_length=1, max_length=50)
    is_enabled: bool | None = None


class InstrumentOut(_Out):
    id: uuid.UUID
    name: str
    kind: InstrumentKind
    model: str | None
    resource: str
    driver: str
    is_enabled: bool
    status: InstrumentStatus
    idn: str | None
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime


# -- samples ----------------------------------------------------------------------------------


class SampleCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    material: str = Field(min_length=1, max_length=100)
    thickness_mm: float = Field(gt=0, le=1000)
    description: str | None = None


class SampleUpdate(BaseModel):
    material: str | None = Field(default=None, min_length=1, max_length=100)
    thickness_mm: float | None = Field(default=None, gt=0, le=1000)
    description: str | None = None


class SampleOut(_Out):
    id: uuid.UUID
    code: str
    material: str
    thickness_mm: float
    description: str | None
    created_at: datetime


# -- methods ----------------------------------------------------------------------------------


class MethodCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    measurement_type: str = "vna_sweep"
    params: dict[str, Any] = Field(default_factory=dict)
    processor: str = "nrw"


class MethodUpdate(BaseModel):
    params: dict[str, Any] | None = None
    processor: str | None = None
    is_archived: bool | None = None


class MethodOut(_Out):
    id: uuid.UUID
    name: str
    measurement_type: str
    params: dict[str, Any]
    processor: str
    is_archived: bool
    created_at: datetime


# -- jobs -------------------------------------------------------------------------------------


class JobCreate(BaseModel):
    method_id: uuid.UUID
    instrument_id: uuid.UUID
    sample_id: uuid.UUID | None = None
    params: dict[str, Any] | None = Field(
        default=None, description="Overrides of the method parameters"
    )


class JobOut(_Out):
    id: uuid.UUID
    method_id: uuid.UUID
    instrument_id: uuid.UUID
    sample_id: uuid.UUID | None
    params: dict[str, Any]
    status: JobStatus
    points_total: int
    points_done: int
    segments_total: int
    segments_done: int
    attempt: int
    error_code: str | None
    error_message: str | None
    heartbeat_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ResultOut(_Out):
    job_id: uuid.UUID
    processor: str
    processor_version: str
    data: dict[str, Any]
    summary: dict[str, Any]
    created_at: datetime
