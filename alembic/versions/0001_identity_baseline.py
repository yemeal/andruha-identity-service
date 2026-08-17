"""Greenfield Identity schema baseline."""

from alembic import op

from app.infrastructure.database.models import Base

revision = "0001_identity_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=False)
