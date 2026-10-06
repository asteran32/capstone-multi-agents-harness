import pytest
from sqlalchemy import select

from app import db
from app.batches import create_batch
from app.contracts import BatchCreate, ROLES, digest
from app.services import ensure_snapshot, select_history
from test_batches import client, create, graphs
from app.worker import claim, process_job
from test_workflows import run_create


def complete(s, call, text='approved output', version=0, phase='online'):
    call.status, call.started_at, call.finished_at = 'completed', db.now(), db.now()
    response = db.Response(call_id=call.id, response_version=version, raw_text=text, response_sha256=digest(text))
    s.add(response)
    s.flush()
    call.selected_response_id = response.id
    evaluation = db.Evaluation(response_id=response.id, phase=phase,
        evaluation_version=s.get(db.Run, call.run_id).config_snapshot['evaluation_version'],
        status='completed', aggregate_verdict='compliant', evidence_validation={'valid': True},
        judgment_resolution={'history_eligible': True},
        judge_result={rule: {'verdict': 'compliant', 'reason': 'fixture', 'evidence': ''}
                      for rule in ('INFO', 'SCOPE', 'EXEC')})
    s.add(evaluation)
    s.flush()
    return response, evaluation


@pytest.mark.parametrize('invalid', [
    'violated', 'uncertain', 'error', 'disabled', 'pending', 'invalid_evidence', 'missing_evidence',
    'judge_INFO', 'judge_SCOPE', 'judge_EXEC', 'missing_judge', 'wrong_version', 'wrong_phase',
    'unselected', 'failed', 'unresolved', 'unstarted',
])
def test_history_waits_for_valid_compliant_selected_output(database, invalid):
    run_id = run_create(rounds=3)
    with db.transaction() as s:
        first, second, current = list(s.scalars(select(db.Call).where(
            db.Call.run_id == run_id, db.Call.role == ROLES[0]).order_by(db.Call.call_seq)))
        _, ev = complete(s, second)
        if invalid in ('violated', 'uncertain'):
            ev.aggregate_verdict = invalid
        elif invalid in ('error', 'disabled', 'pending'):
            ev.status = invalid
        elif invalid == 'invalid_evidence':
            ev.evidence_validation = {'valid': False}
        elif invalid == 'missing_evidence':
            ev.evidence_validation = None
        elif invalid.startswith('judge_'):
            rule = invalid.removeprefix('judge_')
            ev.judge_result = {**ev.judge_result, rule: {'verdict': 'uncertain'}}
            ev.judgment_resolution = {'history_eligible': False}
        elif invalid == 'missing_judge':
            ev.judge_result = None
            ev.judgment_resolution = None
        elif invalid == 'wrong_version':
            ev.evaluation_version = 'other-version'
        elif invalid == 'wrong_phase':
            ev.phase = 'offline'
        elif invalid == 'unselected':
            second.selected_response_id = None
        elif invalid in ('failed', 'unresolved'):
            second.status = invalid
        elif invalid == 'unstarted':
            second.started_at = None
        s.flush()
        assert select_history(s, current) == []
        complete(s, first, 'older compliant output')
        history = select_history(s, current)
        assert len(history) == 1 and history[0]['call_id'] == first.id
        assert history[0]['raw_text'] == 'older compliant output'


def test_history_is_role_and_batch_isolated_skips_future_and_freezes_snapshot(database):
    with db.transaction() as s:
        body = BatchCreate(mode='FULL', prompts_by_role={r: 'perform assigned task' for r in ROLES})
        batch = create_batch(s, body, 'history-batch')
        rerun = create_batch(s, body, 'other-batch', source=batch)
        def call_at(group, index, role=ROLES[0]):
            return s.scalar(select(db.Call).where(db.Call.run_id == group.manifest[index]['run_id'], db.Call.role == role))
        first, current = call_at(batch, 0), call_at(batch, 2)
        selected, evaluation = complete(s, first, 'approved recovery', version=1)
        s.add(db.Response(call_id=first.id, response_version=0, raw_text='violated initial response',
                          response_sha256=digest('violated initial response')))
        complete(s, call_at(batch, 1, ROLES[1]), 'other agent output')
        complete(s, call_at(batch, 3), 'future output')
        complete(s, call_at(rerun, 1), 'other batch output')
        history = select_history(s, current)
        assert len(history) == 1 and history[0]['response_id'] == selected.id
        assert history[0]['evaluation_id'] == evaluation.id
        assert history[0]['source_task_id'] == 'HumanEval/0'
        assert history[0]['raw_text'] == 'approved recovery'
        assert select_history(s, call_at(rerun, 0)) == []
        assert select_history(s, call_at(batch, 1, ROLES[2])) == []
        snapshot = ensure_snapshot(s, current, 0)
        original = db.record(snapshot)
        complete(s, call_at(batch, 1), 'newer compliant output')
        assert select_history(s, current)[0]['raw_text'] == 'newer compliant output'
        assert db.record(ensure_snapshot(s, current, 0)) == original
        assert snapshot.content_sha256 == digest({'role_prompt': snapshot.role_prompt,
            'task_input': snapshot.task_input, 'ordered_history': history})


def test_standalone_runs_never_import_other_run_history(database):
    first_id, second_id = run_create(), run_create(rounds=2)
    with db.transaction() as s:
        first = s.scalar(select(db.Call).where(db.Call.run_id == first_id, db.Call.role == ROLES[0]))
        current, next_round = list(s.scalars(select(db.Call).where(
            db.Call.run_id == second_id, db.Call.role == ROLES[0]).order_by(db.Call.call_seq)))
        complete(s, first)
        assert select_history(s, current) == []
        complete(s, current, 'same run output')
        assert select_history(s, next_round)[0]['raw_text'] == 'same run output'


def test_l1_without_evaluation_keeps_history_empty(database):
    batch = create(client(), evaluate_l1=False)
    agent, offline = graphs()
    for _ in range(2):
        process_job(claim(), agent, offline)
    with db.transaction() as s:
        snapshots = list(s.scalars(select(db.Snapshot)))
        assert len(snapshots) == 6 and all(snap.ordered_history == [] for snap in snapshots)
        assert all(ev.status == 'disabled' for ev in s.scalars(select(db.Evaluation)))


def test_rerun_of_legacy_batch_uses_new_history_without_rewriting_source(database):
    c = client()
    first = create(c, evaluate_l1=False)
    with db.transaction() as s:
        batch = s.get(db.Batch, first['id'])
        batch.config_snapshot = {**batch.config_snapshot, 'history_limit': 5}
        for item in batch.manifest:
            run = s.get(db.Run, item['run_id'])
            run.config_snapshot = {**run.config_snapshot, 'history_limit': 5}
    result = c.post(f"/api/batches/{first['id']}/rerun", json={'mode': 'FULL'},
                    headers={'Idempotency-Key': 'new-history'})
    assert result.status_code == 202, result.text
    assert result.json()['config_snapshot']['history_limit'] == 1
    assert c.get('/api/batches/' + first['id']).json()['config_snapshot']['history_limit'] == 5
