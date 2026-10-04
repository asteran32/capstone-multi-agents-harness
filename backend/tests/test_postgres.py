"""Real PostgreSQL only. Invoked by scripts/verify_postgres.py in an isolated DB."""
import os
from pathlib import Path
import pytest
from sqlalchemy import create_engine, select, text, func
from sqlalchemy.orm import sessionmaker

pytestmark = pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='Dedicated PostgreSQL URL not configured')


def test_postgres_migration_checkpoint_and_restart(monkeypatch):
    from alembic import command
    from alembic.config import Config
    from app import db, graphs
    from app.seed import seed
    from app.config import settings
    from app.worker import postgres_checkpointer, invoke_or_resume
    from app.contracts import RunCreate, ROLES
    from app.services import create_run
    url = os.environ['TEST_POSTGRES_URL']
    assert 'guardrail_test_' in url, 'Use the isolated database created by the verification script'
    engine = create_engine(url)
    monkeypatch.setattr(db,'engine',engine)
    monkeypatch.setattr(db,'Session',sessionmaker(engine,expire_on_commit=False))
    # Settings is intentionally frozen in production; tests replace its DB target only.
    monkeypatch.setitem(settings.__dict__, 'database_url', url)
    command.upgrade(Config(str(Path(__file__).resolve().parents[1]/'alembic.ini')),'head')
    seed()
    with db.transaction() as s:
        task=s.scalar(select(db.Task))
        candidate=s.scalar(select(db.Candidate).where(db.Candidate.task_id==task.id))
        run=create_run(s,RunCreate(task_id=task.id,candidate_id=candidate.id,mode='FULL',
                                  prompts_by_role={r:'[mock:recover]' for r in ROLES}),'pg-restart')
        s.flush()
        call_id=s.scalar(select(db.Call.id).where(db.Call.run_id==run.id).order_by(db.Call.call_seq))
    original=graphs.aggregate_node
    failed=False
    def crash(state):
        nonlocal failed
        result=original(state)
        if not failed:
            failed=True
            raise RuntimeError('simulated death after business commit')
        return result
    monkeypatch.setattr(graphs,'aggregate_node',crash)
    with postgres_checkpointer() as saver:
        with pytest.raises(RuntimeError):
            invoke_or_resume(graphs.agent_graph(saver),{'call_id':call_id},call_id)
    # A new connection and checkpointer read the durable checkpoint, not Python memory.
    with postgres_checkpointer() as saver:
        invoke_or_resume(graphs.agent_graph(saver),{'call_id':call_id},call_id)
    with db.transaction() as s:
        assert s.get(db.Call,call_id).status=='completed'
        assert s.scalar(select(func.count()).select_from(db.Response))==2
        assert s.scalar(select(func.count()).select_from(db.Evaluation))==2
        assert s.scalar(text('SELECT count(*) FROM checkpoint.checkpoints'))>0
    engine.dispose()
