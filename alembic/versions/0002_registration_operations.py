"""Add durable registration process state."""

from alembic import op

from app.infrastructure.database.models.registration_operations import (
    RegistrationOperationORM,
)

revision = "0002_registration_operations"
down_revision = "0001_identity_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    RegistrationOperationORM.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    RegistrationOperationORM.__table__.drop(bind=op.get_bind(), checkfirst=True)
    op.execute("DROP TYPE IF EXISTS registrationstatus")
