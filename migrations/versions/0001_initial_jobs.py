"""initial jobs table

Revision ID: 0001_initial_jobs
Revises:
Create Date: 2026-04-20
"""

from alembic import op

revision = "0001_initial_jobs"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id           UUID PRIMARY KEY,
            status           TEXT NOT NULL,
            phash            TEXT NULL,
            minio_key        TEXT NOT NULL,
            failed_minio_key TEXT NULL,
            result           JSONB NULL,
            error_code       TEXT NULL,
            error_message    TEXT NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT jobs_status_chk
                CHECK (status IN ('PENDING','PROCESSING','SUCCEEDED',
                                  'FAILED_PERMANENT','FAILED_TRANSIENT')),
            CONSTRAINT jobs_succeeded_has_result_chk
                CHECK (status <> 'SUCCEEDED' OR result IS NOT NULL)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS jobs_stale_idx
            ON jobs (status, updated_at)
            WHERE status IN ('PROCESSING','PENDING');
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS jobs_stale_idx;")
    op.execute("DROP TABLE IF EXISTS jobs;")
