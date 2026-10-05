"""Versioned batch analysis settings, separate from immutable run configuration."""
from alembic import op
from sqlalchemy import Column, inspect
from app.db import J

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installs already use current metadata in 0001.
    if 'analysis_settings' not in {c['name'] for c in inspect(op.get_bind()).get_columns('batches', schema='app')}:
        op.add_column('batches', Column('analysis_settings', J, nullable=False, server_default='{}'), schema='app')


def downgrade():
    op.drop_column('batches', 'analysis_settings', schema='app')
