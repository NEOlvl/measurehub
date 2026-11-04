"""Initial schema: users, instruments, methods, samples, jobs, points, results.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, length=16, create_constraint=False)


def _ts(name: str, nullable: bool = False) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.create_table(
        "instruments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("kind", _enum("instrumentkind", "vna"), nullable=False),
        sa.Column("model", sa.String(100)),
        sa.Column("resource", sa.String(255), nullable=False),
        sa.Column("driver", sa.String(50), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "status",
            _enum("instrumentstatus", "online", "offline", "busy", "error"),
            nullable=False,
        ),
        sa.Column("idn", sa.String(255)),
        _ts("last_seen_at", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_instruments"),
        sa.UniqueConstraint("name", name="uq_instruments_name"),
    )
    op.create_table(
        "methods",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("measurement_type", sa.String(50), nullable=False),
        sa.Column("params", JSON_TYPE, nullable=False),
        sa.Column("processor", sa.String(50), nullable=False),
        sa.Column("is_archived", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Uuid()),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_methods"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_methods_created_by_users"),
        sa.UniqueConstraint("name", name="uq_methods_name"),
    )
    op.create_table(
        "samples",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("material", sa.String(100), nullable=False),
        sa.Column("thickness_mm", sa.Numeric(10, 4, asdecimal=False), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("created_by", sa.Uuid()),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_samples"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_samples_created_by_users"),
        sa.UniqueConstraint("code", name="uq_samples_code"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("method_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("sample_id", sa.Uuid()),
        sa.Column("params", JSON_TYPE, nullable=False),
        sa.Column(
            "status",
            _enum(
                "jobstatus",
                "queued",
                "running",
                "processing",
                "cancelling",
                "cancelled",
                "completed",
                "failed",
            ),
            nullable=False,
        ),
        sa.Column("points_total", sa.Integer(), nullable=False),
        sa.Column("points_done", sa.Integer(), nullable=False),
        sa.Column("segments_total", sa.Integer(), nullable=False),
        sa.Column("segments_done", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("celery_task_id", sa.String(64)),
        sa.Column("error_code", sa.String(50)),
        sa.Column("error_message", sa.Text()),
        _ts("heartbeat_at", nullable=True),
        sa.Column("created_by", sa.Uuid()),
        _ts("created_at"),
        _ts("started_at", nullable=True),
        _ts("finished_at", nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.ForeignKeyConstraint(["method_id"], ["methods.id"], name="fk_jobs_method_id_methods"),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instruments.id"], name="fk_jobs_instrument_id_instruments"
        ),
        sa.ForeignKeyConstraint(["sample_id"], ["samples.id"], name="fk_jobs_sample_id_samples"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_jobs_created_by_users"),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_instrument_id_status", "jobs", ["instrument_id", "status"])
    op.create_index("ix_jobs_created_at_desc", "jobs", [sa.text("created_at DESC")])
    op.create_table(
        "measurement_points",
        sa.Column("id", BIGINT_PK, autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("freq_hz", sa.Double(), nullable=False),
        sa.Column("s11_re", sa.Double(), nullable=False),
        sa.Column("s11_im", sa.Double(), nullable=False),
        sa.Column("s21_re", sa.Double(), nullable=False),
        sa.Column("s21_im", sa.Double(), nullable=False),
        _ts("measured_at"),
        sa.PrimaryKeyConstraint("id", name="pk_measurement_points"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name="fk_measurement_points_job_id_jobs",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("job_id", "idx", name="uq_measurement_points_job_id_idx"),
    )
    op.create_table(
        "results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("processor", sa.String(50), nullable=False),
        sa.Column("processor_version", sa.String(20), nullable=False),
        sa.Column("data", JSON_TYPE, nullable=False),
        sa.Column("summary", JSON_TYPE, nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Uuid()),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_results"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name="fk_results_job_id_jobs"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_results_created_by_users"),
    )
    op.create_index("ix_results_job_id", "results", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_results_job_id", table_name="results")
    op.drop_table("results")
    op.drop_table("measurement_points")
    op.drop_index("ix_jobs_created_at_desc", table_name="jobs")
    op.drop_index("ix_jobs_instrument_id_status", table_name="jobs")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("samples")
    op.drop_table("methods")
    op.drop_table("instruments")
    op.drop_table("users")
