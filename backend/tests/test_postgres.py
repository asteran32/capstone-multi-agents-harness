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
    # Also exercise upgrading an existing 0001 installation, whose metadata
    # predates the batches table. All changes are in the disposable test DB.
    db.Batch.__table__.drop(engine)
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num='0001'"))
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
        response_hashes = list(s.scalars(select(db.Response.response_sha256).order_by(db.Response.id)))
    # Upgrade an existing 0003 database; historical rows remain intact and unmeasured.
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE app.evaluations DROP COLUMN judgment_resolution'))
        connection.execute(text('ALTER TABLE app.input_snapshots DROP COLUMN recovery_feedback'))
        connection.execute(text("UPDATE alembic_version SET version_num='0003'"))
    command.upgrade(Config(str(Path(__file__).resolve().parents[1]/'alembic.ini')), 'head')
    with db.transaction() as s:
        assert list(s.scalars(select(db.Response.response_sha256).order_by(db.Response.id))) == response_hashes
        assert all(e.judgment_resolution is None for e in s.scalars(select(db.Evaluation)))
        assert all(x.recovery_feedback is None for x in s.scalars(select(db.Snapshot)))
    # Validate JSONB dependency matching against real PostgreSQL, including
    # L1 generation -> offline evaluation -> next problem and cancellation.
    from app.contracts import BatchCreate
    from app.batches import create_batch, batch_summary, cancel_batch
    from app.worker import claim, process_job
    with db.transaction() as s:
        # The earlier checkpoint test left a queued job; exclude it here.
        for job in s.scalars(select(db.Job)):
            job.status = 'completed'
        batch = create_batch(s, BatchCreate(prompts_by_role={r:'perform assigned task' for r in ROLES}), 'pg-batch')
        batch_id = batch.id
        manifest = batch.manifest
    # Existing 0002 data must survive addition of the non-null settings column.
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE app.batches DROP COLUMN analysis_settings'))
        connection.execute(text("UPDATE alembic_version SET version_num='0002'"))
    command.upgrade(Config(str(Path(__file__).resolve().parents[1]/'alembic.ini')), 'head')
    with db.transaction() as s:
        assert s.get(db.Batch, batch_id).analysis_settings == {}
        assert len(s.get(db.Batch, batch_id).manifest) == 30
    with postgres_checkpointer() as saver:
        agent, offline = graphs.agent_graph(saver), graphs.offline_graph(saver)
        for index, kind in [(0, 'run'), (0, 'offline'), (1, 'run')]:
            job_id = claim()
            with db.transaction() as s:
                job = s.get(db.Job, job_id)
                assert job.run_id == manifest[index]['run_id'] and job.job_type == kind
            assert claim() is None
            process_job(job_id, agent, offline)
        with db.transaction() as s:
            batch = s.get(db.Batch, batch_id)
            assert batch_summary(s, batch)['finished_runs'] == 1
            cancel_batch(s, batch)
    assert claim() is None
    # Exercise the export's read-only repeatable-read transaction and JSONB usage projection.
    import csv
    import io
    from fastapi.testclient import TestClient
    from app.api import app
    client = TestClient(app)
    client.auth = ('researcher', 'test-password')
    analytics = client.get(f'/api/batches/{batch_id}/analytics')
    assert analytics.status_code == 200, analytics.text
    assert analytics.json()['progress']['planned'] == 90
    assert analytics.json()['threshold']['warmup'] == 3
    assert analytics.json()['judge']['schema_checks'] == 3
    updated = client.put(f'/api/batches/{batch_id}/analysis-settings', json={'revision': 0, 'm4_threshold': .2})
    assert updated.status_code == 200 and updated.json()['revision'] == 1
    exported = client.get(f'/api/batches/{batch_id}/results.csv')
    assert exported.status_code == 200, exported.text
    rows = list(csv.DictReader(io.StringIO(exported.content.decode('utf-8-sig'))))
    assert len(rows) == 90
    assert {r['batch_status'] for r in rows} == {'cancelled'}
    assert rows[0]['evaluation_phase'] == 'offline' and rows[0]['initial_verdict'] == 'compliant'
    assert rows[0]['agent_total_tokens'] == rows[0]['judge_total_tokens'] == '0'
    assert rows[-1]['call_status'] == 'cancelled' and rows[-1]['agent_total_tokens'] == ''
    assert rows[0]['csv_schema_version'] == '3' and rows[0]['m4_threshold'] == '0.2'
    assert rows[0]['analysis_revision'] == '1' and rows[0]['initial_schema_valid'] == 'true'
    engine.dispose()
