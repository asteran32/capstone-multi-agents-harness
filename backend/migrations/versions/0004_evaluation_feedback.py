"""Versioned judgment resolution and immutable recovery feedback."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    # 0001 creates current metadata on a fresh installation.
    for table, column in [('evaluations', 'judgment_resolution'), ('input_snapshots', 'recovery_feedback')]:
        if column not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table, schema='app')}:
            op.add_column(table, sa.Column(column, postgresql.JSONB(), nullable=True), schema='app')


def downgrade():
    op.drop_column('input_snapshots', 'recovery_feedback', schema='app')
    op.drop_column('evaluations', 'judgment_resolution', schema='app')
