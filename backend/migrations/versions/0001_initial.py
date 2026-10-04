"""Initial immutable experiment records and queue."""
from alembic import op
from app.db import Base

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE SCHEMA IF NOT EXISTS app')
    op.execute('CREATE SCHEMA IF NOT EXISTS checkpoint')
    Base.metadata.create_all(op.get_bind())
    op.execute('ALTER TABLE app.input_snapshots ADD CONSTRAINT history_limit CHECK (jsonb_array_length(ordered_history) BETWEEN 0 AND 5)')
    for table in ['input_snapshots', 'responses']:
        op.execute(f'ALTER TABLE app.{table} ADD CONSTRAINT response_version_limit CHECK (response_version IN (0,1))')


def downgrade():
    Base.metadata.drop_all(op.get_bind())
    op.execute('DROP SCHEMA app')
    # Checkpoints are intentionally retained; they are managed by LangGraph.
