"""Persistent ordered HumanEval batches; existing runs remain unchanged."""
from alembic import op
from app.db import Batch

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    # 0001 uses current metadata on fresh installs, so creation is idempotent.
    Batch.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    Batch.__table__.drop(op.get_bind(), checkfirst=True)
