"""Persistent job log (job_events).

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
BIGINT_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "job_events",
        sa.Column("id", BIGINT_PK, autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(10), nullable=False),
        sa.Column("event", sa.String(30), nullable=False),
        sa.Column("payload", JSON_TYPE, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_job_events"),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name="fk_job_events_job_id_jobs", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_job_events_job_id_id", "job_events", ["job_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_job_events_job_id_id", table_name="job_events")
    op.drop_table("job_events")
