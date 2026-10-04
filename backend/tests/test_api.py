import json
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func, delete
from app.api import app
from app import db
from app.worker import claim, process_job
from app.graphs import agent_graph, offline_graph
from langgraph.checkpoint.memory import InMemorySaver
from app.seed import seed
from app.config import settings
from app import providers


def request_body(client, mode='Base'):
    task = client.get('/api/tasks').json()[0]
    candidate = client.get(f"/api/tasks/{task['id']}/candidates").json()[0]
    return {'task_id':task['id'],'candidate_id':candidate['id'],'mode':mode,
            'prompts_by_role':{'problem_designer':'describe','code_reviewer':'review','test_runner':'run'}}


def test_humaneval_catalog_upgrade_preserves_existing_run(database):
    # Simulate the previous three-task database before adding the expanded catalog.
    with db.transaction() as s:
        added_ids = list(s.scalars(select(db.Task.id).where(
            db.Task.source_task_id.not_in(['HumanEval/0', 'HumanEval/1', 'HumanEval/2']))))
        s.execute(delete(db.Candidate).where(db.Candidate.task_id.in_(added_ids)))
        s.execute(delete(db.Task).where(db.Task.id.in_(added_ids)))
        original_tasks = {t.source_task_id: t.id for t in s.scalars(select(db.Task))}
        original_candidates = {c.id: c.code_sha256 for c in s.scalars(select(db.Candidate))}
    client = TestClient(app)
    client.auth = ('researcher', 'test-password')
    result = client.post('/api/runs', json=request_body(client), headers={'Idempotency-Key': 'before-upgrade'})
    assert result.status_code == 202
    run_id = result.json()['id']
    before = client.get('/api/runs/' + run_id).json()['config_snapshot']

    seed()
    seed()

    catalog = client.get('/api/tasks').json()
    assert [t['source_task_id'] for t in catalog] == [f'HumanEval/{i}' for i in range(30)]
    assert all(t['id'] == original_tasks[t['source_task_id']] for t in catalog[:3])
    for task in catalog:
        candidates = client.get(f"/api/tasks/{task['id']}/candidates").json()
        assert [c['private_label'] for c in candidates] == ['correct', 'incorrect']
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.Candidate)) == 60
        assert {c.id: c.code_sha256 for c in s.scalars(select(db.Candidate).where(
            db.Candidate.id.in_(original_candidates)))} == original_candidates
    assert client.get('/api/runs/' + run_id).json()['config_snapshot'] == before


def test_auth_and_run_idempotency(database):
    client = TestClient(app)
    assert client.get('/api/tasks').status_code == 401
    client.auth = ('researcher','test-password')
    body = request_body(client)
    result = client.post('/api/runs',json=body,headers={'Idempotency-Key':'one'})
    assert result.status_code == 202
    again = client.post('/api/runs',json=body,headers={'Idempotency-Key':'one'})
    assert result.json() == again.json()
    assert client.post('/api/runs',json={**body,'mode':'FULL'},headers={'Idempotency-Key':'one'}).status_code == 409
    assert client.post('/api/runs',json={**body,'agent_model':'not-allowed'},headers={'Idempotency-Key':'two'}).status_code == 422
    assert client.get('/api/runs?limit=0').status_code == 422
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.Job)) == 1


def test_api_worker_and_offline_job(database):
    client = TestClient(app)
    client.auth = ('researcher','test-password')
    run_id = client.post('/api/runs',json=request_body(client),headers={'Idempotency-Key':'run'}).json()['id']
    saver = InMemorySaver()
    agent, offline = agent_graph(saver), offline_graph(saver)
    job_id = claim()
    process_job(job_id,agent,offline)
    result = client.get('/api/runs/'+run_id).json()
    assert result['status'] == 'completed' and result['finished_calls'] == 3
    calls = client.get(f'/api/runs/{run_id}/calls').json()['items']
    assert len(calls) == 3
    detail = client.get('/api/calls/'+calls[0]['id']).json()
    assert detail['snapshots'] and detail['requests'] and detail['responses']
    assert detail['evaluations'][0]['status'] == 'disabled'
    assert client.get('/api/calls/'+calls[0]['id']+'/logs?limit=2').json()['has_more']
    a=client.post(f'/api/runs/{run_id}/evaluate',headers={'Idempotency-Key':'eval'})
    b=client.post(f'/api/runs/{run_id}/evaluate',headers={'Idempotency-Key':'eval-again'})
    assert a.status_code == 202 and a.json()['id']==b.json()['id']
    process_job(claim(),agent,offline)
    detail = client.get('/api/calls/'+calls[0]['id']).json()
    assert {e['phase'] for e in detail['evaluations']} == {'online','offline'}


@pytest.mark.parametrize('mode,phase', [('L1+L2', 'online'), ('Base', 'offline')])
def test_judge_json_mode_without_json_in_user_data(database, monkeypatch, mode, phase):
    monkeypatch.setitem(settings.__dict__, 'live_models', ('gpt-5.4-nano',))
    monkeypatch.setitem(settings.__dict__, 'openai_key', 'test-provider-key')
    requests = []

    def provider_post(url, **kwargs):
        payload = kwargs['json']
        requests.append(payload)
        assert url == 'https://api.openai.com/v1/responses'
        assert payload['model'] == 'gpt-5.4-nano'
        assert payload['text']['format']['type'] == 'json_object'
        # Reproduce the API's validation of input, separately from instructions.
        if not any('json' in item.get('content', '').lower() for item in payload['input']):
            return httpx.Response(400, json={'error': {
                'type': 'invalid_request_error', 'param': 'input', 'code': None,
                'message': "Response input messages must contain the word 'json' in some form to use 'text.format' of type 'json_object'."}})
        user_content = next(item['content'] for item in payload['input'] if item['role'] == 'user')
        data = json.loads(user_content)
        # Designer/reviewer data should not need a JSON keyword to pass validation.
        if data['role'] != 'test_runner':
            assert 'json' not in user_content.lower()
        return httpx.Response(200, json=providers.mock(payload, {'kind': 'judge', 'data': data}))

    monkeypatch.setattr(providers.httpx, 'post', provider_post)
    client = TestClient(app)
    client.auth = ('researcher', 'test-password')
    body = {**request_body(client, mode), 'judge_model': 'gpt-5.4-nano'}
    created = client.post('/api/runs', json=body, headers={'Idempotency-Key': 'judge-json'})
    assert created.status_code == 202
    run_id = created.json()['id']
    saver = InMemorySaver()
    agent, offline = agent_graph(saver), offline_graph(saver)
    process_job(claim(), agent, offline)
    if phase == 'offline':
        queued = client.post(f'/api/runs/{run_id}/evaluate', headers={'Idempotency-Key': 'judge-json-offline'})
        assert queued.status_code == 202
        process_job(claim(), agent, offline)
    calls = client.get(f'/api/runs/{run_id}/calls?phase={phase}').json()['items']
    assert len(calls) == len(requests) == 3
    for call in calls:
        ev = call['evaluations'][0]
        assert ev['status'] == 'completed', ev['judge_result']
        assert ev['aggregate_verdict'] == 'compliant'
        assert ev['evidence_validation']['valid']


def test_cancel_and_resume_unstarted_run(database):
    client=TestClient(app)
    client.auth = ('researcher','test-password')
    run_id=client.post('/api/runs',json=request_body(client),headers={'Idempotency-Key':'cancel'}).json()['id']
    client.post(f'/api/runs/{run_id}/cancel')
    saver=InMemorySaver();agent=agent_graph(saver);offline=offline_graph(saver)
    process_job(claim(),agent,offline)
    assert client.get('/api/runs/'+run_id).json()['status']=='cancelled'
    assert client.post(f'/api/runs/{run_id}/resume').status_code==202
    process_job(claim(),agent,offline)
    assert client.get('/api/runs/'+run_id).json()['status']=='completed'


def test_worker_infrastructure_failure_remains_resumable(database,monkeypatch):
    from app import graphs
    client=TestClient(app);client.auth=('researcher','test-password')
    run_id=client.post('/api/runs',json=request_body(client,'L1+L2'),headers={'Idempotency-Key':'restart'}).json()['id']
    original=graphs.aggregate_node
    failed=False
    def fail_once(state):
        nonlocal failed
        result=original(state)
        if not failed:
            failed=True
            raise RuntimeError('checkpoint write interrupted')
        return result
    monkeypatch.setattr(graphs,'aggregate_node',fail_once)
    saver=InMemorySaver();offline=offline_graph(saver)
    process_job(claim(),graphs.agent_graph(saver),offline)
    assert client.get('/api/runs/'+run_id).json()['status']=='failed'
    assert client.post(f'/api/runs/{run_id}/resume').status_code==202
    process_job(claim(),graphs.agent_graph(saver),offline)
    assert client.get('/api/runs/'+run_id).json()['status']=='completed'
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.Response))==3
        assert s.scalar(select(func.count()).select_from(db.Evaluation))==3
