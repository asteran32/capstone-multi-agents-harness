import json
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select, func, delete

from app.api import app
from app import db
from app.contracts import ROLES, digest
from app.graphs import agent_graph, offline_graph
from app.worker import claim, process_job


def client():
    result = TestClient(app)
    result.auth = ('researcher', 'test-password')
    return result


def body(mode='L1', **extra):
    return {'mode': mode, 'prompts_by_role': {r: 'perform assigned task' for r in ROLES}, **extra}


def create(c, mode='L1', key='batch', **extra):
    result = c.post('/api/batches', json=body(mode, **extra), headers={'Idempotency-Key': key})
    assert result.status_code == 202, result.text
    return result.json()


def graphs():
    saver = InMemorySaver()
    return agent_graph(saver), offline_graph(saver)


@pytest.mark.parametrize('mode', ['L1', 'L1+L2', 'FULL'])
@pytest.mark.parametrize('candidate_label', ['correct', 'incorrect'])
def test_batch_executes_all_100_with_latest_compliant_output_per_role(database, mode, candidate_label):
    c = client()
    batch = create(c, mode, candidate_label=candidate_label)
    assert batch['config_snapshot']['candidate_label'] == candidate_label
    assert batch['total_runs'] == 100 and batch['finished_runs'] == 0
    assert [item['source_task_id'] for item in batch['items']] == [f'HumanEval/{i}' for i in range(100)]
    agent, offline = graphs()
    observed = []
    while (job_id := claim()):
        with db.transaction() as s:
            job = s.get(db.Job, job_id)
            observed.append((job.run_id, job.job_type))
        # The next batch member cannot overtake this claimed job.
        assert claim() is None
        process_job(job_id, agent, offline)
    expected = [(item['run_id'], kind) for item in batch['items']
                for kind in (['run', 'offline'] if mode == 'L1' else ['run'])]
    assert observed == expected
    detail = c.get('/api/batches/' + batch['id']).json()
    assert detail['status'] == 'completed' and detail['finished_runs'] == 100 and detail['error_runs'] == 0
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.Run)) == 100
        assert s.scalar(select(func.count()).select_from(db.Call)) == 300
        for index, item in enumerate(batch['items']):
            run = s.get(db.Run, item['run_id'])
            candidate = s.get(db.Candidate, run.config_snapshot['candidate_id'])
            assert candidate.private_label == candidate_label
            assert 'candidate_label' not in run.config_snapshot
            assert run.config_snapshot['batch_id'] == batch['id']
            assert run.config_snapshot['history_limit'] == 1
            for call in s.scalars(select(db.Call).where(db.Call.run_id == run.id)):
                snap = s.scalar(select(db.Snapshot).where(db.Snapshot.call_id == call.id))
                if call.role != 'problem_designer':
                    assert snap.task_input['candidate_code'] == candidate.code
                assert len(snap.ordered_history) == (1 if index else 0)
                if index:
                    history = snap.ordered_history[0]
                    previous = s.get(db.Call, history['call_id'])
                    assert previous.role == call.role and previous.run_id == batch['items'][index - 1]['run_id']
                    assert history['source_task_id'] == f'HumanEval/{index - 1}'
                    assert history['raw_text'] == s.get(db.Response, previous.selected_response_id).raw_text
                    assert set(history) == {'call_id', 'source_task_id', 'response_id', 'response_version', 'evaluation_id', 'raw_text'}
                    evaluation = s.get(db.Evaluation, history['evaluation_id'])
                    assert evaluation.phase == ('offline' if mode == 'L1' else 'online')
                for request in s.scalars(select(db.ModelRequest).where(db.ModelRequest.call_id == call.id)):
                    payload = json.dumps(request.request_payload)
                    assert 'candidate_label' not in payload and 'private_label' not in payload
                    data = json.loads(next(message['content'] for message in request.request_payload['input']
                                           if message.get('role') == 'user'))
                    if request.evaluation_id is None:
                        assert data['history'] == snap.ordered_history
                        assert request.request_payload['instructions'] == snap.role_prompt
                        assert ('only the current tool_result proves execution' in request.request_payload['instructions']) == bool(index)
                    else:
                        assert 'history' not in data
        assert s.scalar(select(func.count()).select_from(db.Evaluation).where(
            db.Evaluation.phase == 'offline')) == (300 if mode == 'L1' else 0)


def test_batch_request_idempotency_auth_and_validation(database):
    c = client()
    assert TestClient(app).post('/api/batches', json=body()).status_code == 401
    assert c.post('/api/batches', json=body()).status_code == 422
    assert c.post('/api/batches', json=body('Base'), headers={'Idempotency-Key': 'invalid'}).status_code == 422
    assert c.post('/api/batches', json=body(agent_model='unregistered'),
                  headers={'Idempotency-Key': 'bad-model'}).status_code == 422
    assert c.post('/api/batches', json=body(policy_raw='roles: ['),
                  headers={'Idempotency-Key': 'bad-policy'}).status_code == 422
    assert c.post('/api/batches', json=body(candidate_label='unknown'),
                  headers={'Idempotency-Key': 'bad-candidate'}).status_code == 422
    first = create(c)
    assert first['config_snapshot']['candidate_label'] == 'correct'
    assert create(c)['id'] == first['id']
    assert create(c, candidate_label='correct')['id'] == first['id']
    assert c.post('/api/batches', json=body('FULL'), headers={'Idempotency-Key': 'batch'}).status_code == 409
    assert c.post('/api/batches', json=body(candidate_label='incorrect'),
                  headers={'Idempotency-Key': 'batch'}).status_code == 409
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.Batch)) == 1
        assert s.scalar(select(func.count()).select_from(db.Run)) == 100
    assert c.get('/api/batches').json()['total'] == 1
    assert c.get('/api/batches?limit=0').status_code == 422


@pytest.mark.parametrize('candidate_label', ['correct', 'incorrect'])
@pytest.mark.parametrize('candidate_count', [0, 2])
def test_missing_or_duplicate_candidate_rejects_entire_batch(database, candidate_label, candidate_count):
    with db.transaction() as s:
        task = s.scalar(select(db.Task).where(db.Task.source_task_id == 'HumanEval/99'))
        candidate = s.scalar(select(db.Candidate).where(db.Candidate.task_id == task.id,
                                                       db.Candidate.private_label == candidate_label))
        if candidate_count == 0:
            s.execute(delete(db.Candidate).where(db.Candidate.id == candidate.id))
        else:
            code = candidate.code + '\n# additional candidate\n'
            s.add(db.Candidate(task_id=task.id, code=code, code_sha256=digest(code), private_label=candidate_label))
    result = client().post('/api/batches', json=body(candidate_label=candidate_label),
                           headers={'Idempotency-Key': 'invalid-candidates'})
    assert result.status_code == 422
    assert 'HumanEval/99' in result.json()['detail']
    assert ('정답 후보' if candidate_label == 'correct' else '오답 후보') in result.json()['detail']
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.Batch)) == 0
        assert s.scalar(select(func.count()).select_from(db.Run)) == 0
        assert s.scalar(select(func.count()).select_from(db.Job)) == 0


@pytest.mark.parametrize('candidate_label', ['correct', 'incorrect'])
def test_rerun_changes_only_mode_and_creates_new_runs(database, candidate_label):
    c = client()
    first = create(c, rounds=2, evaluate_l1=False, candidate_label=candidate_label)
    assert create(c, rounds=2, evaluate_l1=False, candidate_label=candidate_label)['id'] == first['id']
    assert c.get('/api/batches').json()['items'][0]['config_snapshot']['candidate_label'] == candidate_label
    result = c.post(f"/api/batches/{first['id']}/rerun", json={'mode': 'FULL'},
                    headers={'Idempotency-Key': 'rerun'})
    assert result.status_code == 202, result.text
    second = result.json()
    assert second['source_batch_id'] == first['id']
    assert second['config_snapshot'] == {**first['config_snapshot'], 'mode': 'FULL'}
    for a, b in zip(first['items'], second['items']):
        assert a['run_id'] != b['run_id']
        for field in ['task_id', 'candidate_id', 'task_sha256', 'candidate_sha256', 'source_version']:
            assert a[field] == b[field]
    assert c.post(f"/api/batches/{first['id']}/rerun", json={'mode': 'FULL'},
                  headers={'Idempotency-Key': 'rerun'}).json()['id'] == second['id']
    assert c.get('/api/batches/' + first['id']).json()['mode'] == 'L1'
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.Job)) == 200


def test_legacy_batch_rerun_defaults_to_correct_candidates(database):
    c = client()
    first = create(c)
    with db.transaction() as s:
        batch = s.get(db.Batch, first['id'])
        batch.config_snapshot = {k: v for k, v in batch.config_snapshot.items() if k != 'candidate_label'}
    result = c.post(f"/api/batches/{first['id']}/rerun", json={'mode': 'FULL'},
                    headers={'Idempotency-Key': 'legacy-rerun'})
    assert result.status_code == 202, result.text
    assert result.json()['config_snapshot']['candidate_label'] == 'correct'
    assert [item['candidate_id'] for item in result.json()['items']] == [item['candidate_id'] for item in first['items']]
    assert 'candidate_label' not in c.get('/api/batches/' + first['id']).json()['config_snapshot']


@pytest.mark.parametrize('candidate_label', ['correct', 'incorrect'])
def test_30_task_batch_rerun_preserves_original_manifest_after_expansion(database, monkeypatch, candidate_label):
    from app import batches
    c = client()
    with monkeypatch.context() as old_catalog:
        old_catalog.setattr(batches, 'dataset_task_ids', lambda: tuple(f'HumanEval/{index}' for index in range(30)))
        first = create(c, candidate_label=candidate_label)
    assert first['total_runs'] == 30
    original = c.get('/api/batches/' + first['id']).json()
    result = c.post(f"/api/batches/{first['id']}/rerun", json={'mode': 'FULL'},
                    headers={'Idempotency-Key': 'legacy-size-rerun'})
    assert result.status_code == 202, result.text
    second = result.json()
    assert second['total_runs'] == 30
    assert second['config_snapshot']['candidate_label'] == candidate_label
    assert [(item['task_id'], item['candidate_id']) for item in second['items']] == [
        (item['task_id'], item['candidate_id']) for item in first['items']]
    assert c.get('/api/batches/' + first['id']).json() == original
    assert create(c, key='expanded', candidate_label=candidate_label)['total_runs'] == 100


@pytest.mark.parametrize('candidate_label', ['correct', 'incorrect'])
def test_rerun_refuses_changed_source_data(database, candidate_label):
    c = client()
    batch = create(c, candidate_label=candidate_label)
    with db.transaction() as s:
        candidate = s.get(db.Candidate, batch['items'][0]['candidate_id'])
        candidate.code += '\n# changed'
    result = c.post(f"/api/batches/{batch['id']}/rerun", json={'mode': 'L1+L2'},
                    headers={'Idempotency-Key': 'changed'})
    assert result.status_code == 422
    assert c.get('/api/batches').json()['total'] == 1


def test_batch_cancel_stops_claimed_and_unstarted_jobs(database):
    c = client()
    batch = create(c)
    job_id = claim()
    result = c.post(f"/api/batches/{batch['id']}/cancel").json()
    assert result['status'] == 'cancel_requested'
    agent, offline = graphs()
    process_job(job_id, agent, offline)
    assert claim() is None
    detail = c.get('/api/batches/' + batch['id']).json()
    assert detail['status'] == 'cancelled' and detail['finished_runs'] == 100
    assert all(item['status'] == 'cancelled' for item in detail['items'])
    assert c.post(f"/api/runs/{batch['items'][0]['run_id']}/resume").status_code == 409
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.ModelRequest)) == 0


def test_cancel_during_offline_preserves_completed_generation(database):
    c = client()
    batch = create(c)
    agent, offline = graphs()
    process_job(claim(), agent, offline)
    offline_job = claim()
    c.post(f"/api/batches/{batch['id']}/cancel")
    process_job(offline_job, agent, offline)
    result = c.get('/api/batches/' + batch['id']).json()
    assert result['status'] == 'cancelled'
    assert result['items'][0]['status'] == 'completed'
    assert result['items'][0]['offline_status'] == 'cancelled'
    assert claim() is None


def test_generation_failure_is_visible_and_next_problem_runs(database):
    c = client()
    batch = create(c, evaluate_l1=False)
    agent, offline = graphs()

    class BrokenGraph:
        def get_state(self, config):
            raise RuntimeError('simulated worker failure')

    process_job(claim(), BrokenGraph(), offline)
    next_id = claim()
    with db.transaction() as s:
        assert s.get(db.Job, next_id).run_id == batch['items'][1]['run_id']
    process_job(next_id, agent, offline)
    detail = c.get('/api/batches/' + batch['id']).json()
    assert detail['error_runs'] == 1 and detail['finished_runs'] == 2
    assert detail['items'][0]['status'] == 'failed'
    assert detail['items'][1]['status'] == 'completed'


def test_individually_cancelled_run_is_not_reported_as_success(database):
    c = client()
    batch = create(c, evaluate_l1=False)
    c.post(f"/api/runs/{batch['items'][0]['run_id']}/cancel")
    agent, offline = graphs()
    process_job(claim(), agent, offline)
    detail = c.get('/api/batches/' + batch['id']).json()
    assert detail['finished_runs'] == 1 and detail['error_runs'] == 1
    assert detail['items'][0]['status'] == 'cancelled'
    assert detail['items'][0]['has_errors']
