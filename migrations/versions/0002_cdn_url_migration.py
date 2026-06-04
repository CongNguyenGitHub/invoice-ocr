"""rename minio_key to image_url, drop failed_minio_key

Revision ID: 0002_cdn_url_migration
Revises: 0001_initial_jobs
Create Date: 2026-06-04
"""
from alembic import op

revision = "0002_cdn_url_migration"
down_revision = "0001_initial_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename minio_key → image_url (breaking — clean slate for new system)
    op.execute("ALTER TABLE jobs RENAME COLUMN minio_key TO image_url;")
    op.execute("ALTER TABLE jobs ALTER COLUMN image_url TYPE VARCHAR(512);")

    # Drop failed_minio_key — failures now just store the URL in image_url
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS failed_minio_key;")


def downgrade() -> None:
    # Reverse: add back failed_minio_key and rename image_url → minio_key
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS failed_minio_key TEXT NULL;")
    op.execute("ALTER TABLE jobs ALTER COLUMN image_url TYPE TEXT;")
    op.execute("ALTER TABLE jobs RENAME COLUMN image_url TO minio_key;")
