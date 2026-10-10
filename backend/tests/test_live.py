import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select, func

from app import db, runtime, providers
from app.api import app
from app.config import settings
from app.contracts import ROLES, digest
from app.graphs import agent_graph, offline_graph
from app.live import role_metrics
from app.live_problems import PROBLEMS, source_record
from app.worker import claim, process_job


def client():
    result = TestClient(app)
    result.auth = ('researcher', 'test-password')
    return result


def create(c, key='live', **body):
    result = c.post('/api/live/sessions', json=body, headers={'Idempotency-Key': key})
    assert result.status_code == 202, result.text
    return result.json()


def drain():
    saver = InMemorySaver()
    agent, offline = agent_graph(saver), offline_graph(saver)
    while job_id := claim():
        process_job(job_id, agent, offline)


def submit(c, session, code=None, key='submit'):
    return c.post(f"/api/live/sessions/{session['id']}/submissions",
                  json={'code': code or session['problem']['starter_code']}, headers={'Idempotency-Key': key})


@pytest.mark.parametrize('mode', ['L1+L2', 'FULL'])
def test_live_generates_problem_then_only_reviews_and_tests_submission(database, mode):
    c = client()
    session = create(c, mode=mode)
    assert session['status'] == 'generating' and session['submission'] is None
    assert session['metrics']['code_reviewer']['info_pass']['rate'] is None
    assert len(c.get('/api/tasks').json()) == 100
    assert 'canonical_solution' not in json.dumps(session) and '"test"' not in json.dumps(session)
    assert submit(c, session).status_code == 409
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.Call)) == 1
        assert s.scalar(select(func.count()).select_from(db.ToolExecution)) == 0
    drain()
    session = c.get('/api/live/sessions/' + session['id']).json()
    assert session['status'] == 'ready'
    statement = session['designer']['response']['parsed_output']
    assert statement['signature'] + '\n    pass\n' == session['problem']['starter_code']
    assert statement['description'] != 'Mock 문제 설명입니다.'
    code = statement['signature'] + '\n    return None\n'
    result = submit(c, session, code)
    assert result.status_code == 202, result.text
    queued = result.json()
    assert queued['submission']['code'] == code
    assert set(queued['submission']['agents']) == {'code_reviewer', 'test_runner'}
    drain()
    detail = c.get('/api/live/sessions/' + session['id']).json()
    assert detail['submission']['status'] == 'completed'
    assert detail['submission']['agents']['test_runner']['tool_result']['status'] == 'not_executed'
    assert detail['submission']['agents']['test_runner']['tool_result']['tests'] is None
    for role in ROLES:
        metric = detail['metrics'][role]
        assert metric['calls'] == metric['evaluated'] == 1
        assert metric['compliant_delivery']['rate'] == metric['judge_validity']['rate'] == 1
        assert metric['window']['warmup'] and metric['window']['window_count'] == 1
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.Call)) == 3
        run = s.get(db.Run, detail['submission']['id'])
        source = s.get(db.Run, session['id'])
        assert run.config_snapshot['evaluation_version'] == source.config_snapshot['evaluation_version']
        assert run.config_snapshot['policy'] == source.config_snapshot['policy']
        candidate = s.get(db.Candidate, run.config_snapshot['candidate_id'])
        assert candidate.code == code and candidate.private_label == 'unlabeled'
        for call in s.scalars(select(db.Call).where(db.Call.run_id == run.id)):
            snap = s.scalar(select(db.Snapshot).where(db.Snapshot.call_id == call.id))
            assert snap.task_input['candidate_code'] == code
            assert 'canonical_solution' not in snap.task_input
            if call.role == 'code_reviewer':
                assert 'test' not in snap.task_input
            for request in s.scalars(select(db.ModelRequest).where(db.ModelRequest.call_id == call.id)):
                assert 'canonical_solution' not in json.dumps(request.request_payload)
                assert 'private_label' not in json.dumps(request.request_payload)
                if call.role == 'test_runner' and request.request_payload.get('tools'):
                    assert request.request_payload['tool_choice'] == 'required'
        tool = s.scalar(select(db.ToolExecution))
        assert tool.arguments['code'] == code and tool.arguments['code_sha256'] == digest(code)
        task = s.get(db.Task, run.config_snapshot['task_id'])
        assert tool.arguments['test'] == task.source_record['test']


def test_live_auth_validation_and_creation_idempotency(database):
    assert TestClient(app).post('/api/live/sessions', json={}).status_code == 401
    c = client()
    assert c.post('/api/live/sessions', json={}).status_code == 422
    for body in ({'mode': 'Base'}, {'agent_model': 'unregistered'}, {'judge_model': 'unregistered'}, {'task_id': db.uid()}):
        assert c.post('/api/live/sessions', json=body, headers={'Idempotency-Key': 'invalid'}).status_code == 422
    first = create(c)
    assert create(c)['id'] == first['id']
    assert c.post('/api/live/sessions', json={'mode': 'L1+L2'}, headers={'Idempotency-Key': 'live'}).status_code == 409
    assert c.get('/api/live/sessions/' + db.uid()).status_code == 404
    for code in ('', '   \n ', 'a' * 20001):
        assert c.post(f"/api/live/sessions/{first['id']}/submissions", json={'code': code},
                      headers={'Idempotency-Key': 'invalid-code'}).status_code == 422
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.Run)) == 1
        assert s.scalar(select(func.count()).select_from(db.Task)) == 101
        assert s.scalar(select(func.count()).select_from(db.Job)) == 1


def test_live_submission_retries_history_and_single_inflight_submission(database):
    c = client()
    session = create(c)
    drain()
    first = submit(c, session)
    assert first.status_code == 202
    first_id = first.json()['submission']['id']
    assert submit(c, session).json()['submission']['id'] == first_id
    assert submit(c, session, session['problem']['starter_code'] + '# changed').status_code == 409
    assert submit(c, session, key='another').status_code == 409
    drain()
    second_code = session['problem']['starter_code'] + '\n# second submission\n'
    second = submit(c, session, second_code, key='second')
    assert second.status_code == 202
    assert second.json()['submission']['id'] != first_id
    drain()
    historical = c.get(f"/api/live/sessions/{session['id']}?submission_id={first_id}").json()
    assert historical['submission']['code'] == session['problem']['starter_code']
    assert len(historical['submissions']) == 2
    assert historical['metrics']['code_reviewer']['window']['window_count'] == 2
    assert historical['metrics']['problem_designer']['calls'] == 1
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.Run)) == 3
        assert s.scalar(select(func.count()).select_from(db.Job)) == 3


def test_live_submissions_keep_session_prompts_when_defaults_change(database, monkeypatch):
    from app import live
    c = client()
    frozen_prompts = dict(live.PROMPTS)
    session = create(c)
    drain()
    monkeypatch.setattr(live, 'PROMPTS', {role: 'Changed default request' for role in ROLES})
    response = submit(c, session)
    assert response.status_code == 202, response.text
    submission_id = response.json()['submission']['id']
    with db.transaction() as s:
        run = s.get(db.Run, submission_id)
        assert run.config_snapshot['prompts_by_role'] == frozen_prompts
        for call in s.scalars(select(db.Call).where(db.Call.run_id == submission_id)):
            assert call.user_prompt == frozen_prompts[call.role]
    drain()
    assert c.get('/api/live/sessions/' + session['id']).json()['submission']['status'] == 'completed'


def test_new_problem_changes_spec_and_rejects_cross_session_results(database):
    c = client()
    first = create(c)
    drain()
    submitted = submit(c, first).json()['submission']['id']
    second = create(c, key='new', previous_session_id=first['id'])
    assert second['problem']['title'] != first['problem']['title']
    assert c.get(f"/api/live/sessions/{second['id']}?submission_id={submitted}").status_code == 404
    assert c.post('/api/live/sessions', json={'previous_session_id': db.uid()},
                  headers={'Idempotency-Key': 'missing'}).status_code == 404
    assert submit(c, second).status_code == 409


def test_live_blocks_problem_that_fails_role_evaluation(database, monkeypatch):
    c = client()
    original = providers.mock
    def wrong_signature(payload, context):
        response = original(payload, context)
        if context['kind'] == 'agent' and context.get('role') == 'problem_designer':
            obj = json.loads(response['output'][0]['content'][0]['text'])
            obj['signature'] = 'def unrelated_function():'
            response['output'][0]['content'][0]['text'] = json.dumps(obj)
        return response
    monkeypatch.setattr(runtime, 'respond', wrong_signature)
    session = create(c, mode='L1+L2')
    drain()
    result = c.get('/api/live/sessions/' + session['id']).json()
    assert result['status'] == 'blocked' and result['designer']['withheld']
    assert result['designer']['response'] is None
    assert submit(c, result).status_code == 409
    assert result['metrics']['problem_designer']['compliant_delivery']['rate'] == 0


def test_live_delivers_exact_user_code_to_existing_sandbox(database, monkeypatch):
    c = client()
    monkeypatch.setitem(settings.__dict__, 'sandbox_mode', 'docker')
    session = create(c)
    drain()
    captured = []
    failure = {'status': 'fail', 'exit_code': 1, 'tests': 1, 'failures': 1, 'errors': 0}
    class SandboxClient:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, json, headers):
            captured.append(json)
            return httpx.Response(200, json={'state': 'completed', 'result': failure}, request=httpx.Request('POST', url))
    code = session['problem']['starter_code'] + '\n# preserved verbatim\n'
    assert submit(c, session, code).status_code == 202
    # Only the sandbox HTTP transport is replaced; the graph/tool/Judge paths run normally.
    monkeypatch.setattr(runtime.httpx, 'Client', SandboxClient)
    drain()
    assert len(captured) == 1 and captured[0]['code'] == code
    assert captured[0]['code_sha256'] == digest(code)
    result = c.get('/api/live/sessions/' + session['id']).json()
    runner = result['submission']['agents']['test_runner']
    assert runner['tool_result'] == failure
    assert runner['response']['parsed_output'] == failure
    assert result['metrics']['test_runner']['compliant_delivery']['rate'] == 1


def test_live_rejects_changed_problem_before_creating_submission(database):
    c = client()
    session = create(c)
    drain()
    with db.transaction() as s:
        run = s.get(db.Run, session['id'])
        task = s.get(db.Task, run.config_snapshot['task_id'])
        task.source_record = {**task.source_record, 'test': 'changed'}
    assert submit(c, session).status_code == 422
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.Run)) == 1


def test_live_metrics_keep_uncertainty_recovery_and_missing_denominators_distinct():
    calls, responses, evaluations = [], {}, {}
    for index, verdict in enumerate(['compliant', 'violated', 'uncertain', 'compliant', 'violated', 'compliant']):
        response_id = f'response-{index}'
        calls.append(SimpleNamespace(id=f'call-{index}', role='code_reviewer', selected_response_id=response_id))
        responses[response_id] = SimpleNamespace(id=response_id, call_id=f'call-{index}', response_version=0)
        rules = {rule: {'verdict': verdict} for rule in ('INFO', 'SCOPE', 'EXEC')}
        evaluations[response_id] = SimpleNamespace(id=f'ev-{index}', aggregate_verdict=verdict, status='completed',
            judgment_resolution={'effective_rules': rules}, judge_result=rules,
            lexical_result={'verdict': 'compliant'}, evidence_validation={'valid': True})
    calls.append(SimpleNamespace(id='pending', role='code_reviewer', selected_response_id=None))
    # A compliant recovery is delivered but does not become another observation in M4.
    responses['recovery'] = SimpleNamespace(id='recovery', call_id='call-1', response_version=1)
    evaluations['recovery'] = SimpleNamespace(aggregate_verdict='compliant', status='completed')
    calls[1].selected_response_id = 'recovery'
    values = role_metrics(calls, responses, evaluations)
    reviewer = values['code_reviewer']
    assert reviewer['calls'] == 7 and reviewer['evaluated'] == 6
    assert reviewer['compliant_delivery']['rate'] == 4 / 7
    assert reviewer['judge_compliance']['rate'] == 3 / 5
    assert reviewer['window']['role_violation_rate'] == 2 / 4
    assert reviewer['window']['decision_coverage'] == 4 / 5
    assert not reviewer['window']['warmup']
    assert values['test_runner']['judge_validity']['rate'] is None


@pytest.mark.parametrize('problem', PROBLEMS, ids=lambda problem: problem['key'])
def test_trusted_easy_specs_have_consistent_reference_tests_and_starters(problem):
    source = source_record(problem, 'Live/test')
    # Execute repository-owned reference code only, never submitted user code.
    namespace = {}
    exec(source['prompt'] + source['canonical_solution'], namespace)
    exec(source['test'], namespace)
    namespace['check'](namespace[source['entry_point']])
    with pytest.raises(AssertionError):
        namespace['check'](lambda *args: None)
    compile(problem['signature'] + '\n    pass\n', 'starter.py', 'exec')
