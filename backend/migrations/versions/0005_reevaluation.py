"""Independent fixed-corpus reviews, evaluator executions and request audit."""
from alembic import op
from app.db import ReviewSet, ReviewCase, Reevaluation, ReevaluationResult, ReevaluationRequest

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    for model in (ReviewSet, ReviewCase, Reevaluation, ReevaluationResult, ReevaluationRequest):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    for model in (ReevaluationRequest, ReevaluationResult, Reevaluation, ReviewCase, ReviewSet):
        model.__table__.drop(op.get_bind())
