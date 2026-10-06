import csv
import io
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select

from app import db
from app.api import app
from app.contracts import ROLES
from app.graphs import agent_graph, offline_graph
from app.worker import claim, process_job


def client():
    c = TestClient(app)
    c.auth = ('researcher', 'test-password')
    return c


def create(c, mode='L1', **extra):
    response = c.post('/api/batches', headers={'Idempotency-Key': str(uuid4())}, json={
        'mode': mode, 'prompts_by_role': {r: '검토 요청, "내용"\n다음 줄' for r in ROLES}, **extra})
    assert response.status_code == 202, response.text
    return response.json()


def download(c, batch):
    response = c.get(f"/api/batches/{batch['id']}/results.csv")
    assert response.status_code == 200, response.text
    assert response.headers['content-type'] == 'text/csv; charset=utf-8'
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['content-disposition'].startswith('attachment; filename="guardrail-batch-')
    assert batch['id'] in response.headers['content-disposition']
    assert response.content.startswith(b'\xef\xbb\xbf')
    return list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'), newline='')))


def process_first(mode):
    saver = InMemorySaver()
    agent, offline = agent_graph(saver), offline_graph(saver)
    process_job(claim(), agent, offline)
    if mode == 'L1':
        process_job(claim(), agent, offline)


@pytest.mark.parametrize('mode', ['L1', 'L1+L2', 'FULL'])
def test_export_partial_batch_preserves_order_phase_and_scope(database, mode):
    c = client()
    batch = create(c, mode)
    process_first(mode)
    other = create(c, mode)
    with db.transaction() as s:
        before = s.scalar(select(func.count()).select_from(db.ModelRequest))
    rows = download(c, batch)
    assert len(rows) == 90
    assert [(r['source_task_id'], r['round_index'], r['role']) for r in rows] == [
        (f'HumanEval/{i}', '1', role) for i in range(30) for role in ROLES]
    assert {r['batch_id'] for r in rows} == {batch['id']}
    assert not {r['run_id'] for r in rows} & {item['run_id'] for item in other['items']}
    assert {r['batch_status'] for r in rows} == {'running'}
    assert {r['evaluation_phase'] for r in rows} == {'offline' if mode == 'L1' else 'online'}
    assert len({r['exported_at'] for r in rows}) == 1
    for row in rows[:3]:
        assert row['call_status'] == 'completed'
        assert row['initial_verdict'] == row['final_verdict'] == 'compliant'
        assert row['initial_evaluation_status'] == 'completed'
        assert row['initial_response_text'] == row['final_response_text']
        assert row['selected_response_version'] == '0'
        assert row['agent_input_tokens'] == row['agent_output_tokens'] == row['agent_total_tokens'] == '0'
        assert row['judge_request_count'] == row['judge_usage_reported_requests'] == '1'
        assert row['judge_total_tokens'] == '0'
        assert row['user_prompt'] == '검토 요청, "내용"\n다음 줄'
        assert float(row['generation_duration_ms']) >= 0
        assert row['initial_window_count'] == '1' and row['initial_warmup'] == 'true'
    assert rows[2]['agent_request_count'] == '2'
    assert json.loads(rows[2]['tool_results'])[0]['result']['mock'] is True
    assert rows[3]['call_status'] == 'queued'
    assert rows[3]['initial_evaluation_status'] == 'not_evaluated'
    assert rows[3]['agent_total_tokens'] == rows[3]['initial_verdict'] == rows[3]['generation_duration_ms'] == ''
    assert None not in rows[0]  # CSV quotes multiline fields without adding columns.
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.ModelRequest)) == before


def test_export_recovery_usage_retries_missing_values_and_formula_text(database):
    c = client()
    batch = create(c, 'FULL', prompts_by_role={r: '[mock:recover]' for r in ROLES})
    process_first('FULL')
    with db.transaction() as s:
        call = s.scalar(select(db.Call).where(db.Call.run_id == batch['items'][0]['run_id'], db.Call.role == ROLES[0]))
        call.user_prompt = ' \t=HYPERLINK("example")'
        response = s.scalar(select(db.Response).where(db.Response.call_id == call.id, db.Response.response_version == 1))
        response.raw_text = '+테스트, "인용"\n줄바꿈'
        agent = list(s.scalars(select(db.ModelRequest).where(db.ModelRequest.call_id == call.id,
                            db.ModelRequest.evaluation_id.is_(None)).order_by(db.ModelRequest.created_at)))
        assert len(agent) == 2
        agent[0].usage = {'input_tokens': 11, 'output_tokens': 7}
        agent[1].usage = None
        agent[1].response_payload = {'usage': {'input_tokens': 2, 'output_tokens': 3}}
        s.add(db.ModelRequest(call_id=call.id, input_snapshot_id=agent[0].input_snapshot_id,
                             operation_key=agent[0].operation_key, attempt_number=2, request_payload={},
                             status='failed', error={'code': 'synthetic transport error'}))
    row = download(c, batch)[0]
    assert row['initial_verdict'] == 'violated' and row['final_verdict'] == 'compliant'
    assert row['recovery_attempted'] == 'true' and row['response_count'] == '2'
    assert row['selected_response_version'] == '1' and row['reset_reason'] == 'confirmed_violation_feedback'
    assert row['recovery_strategy'] == 'feedback' and row['csv_schema_version'] == '4'
    assert json.loads(row['recovery_feedback'])['confirmed_violations'][0]['source'] == 'lexical'
    assert row['recovery_verdict'] == 'compliant'
    assert json.loads(row['recovery_judgment_resolution'])['aggregate_verdict'] == 'compliant'
    assert row['user_prompt'] == '\' \t=HYPERLINK("example")'
    assert row['recovery_response_text'] == row['final_response_text'] == '\'+테스트, "인용"\n줄바꿈'
    assert row['agent_request_count'] == '3' and row['agent_usage_reported_requests'] == '2'
    assert row['agent_failed_requests'] == '1'
    assert (row['agent_input_tokens'], row['agent_output_tokens'], row['agent_total_tokens']) == ('13', '10', '23')
    assert row['judge_request_count'] == '2' and row['judge_total_tokens'] == '0'
    assert json.loads(row['request_errors'])[0]['error']['code'] == 'synthetic transport error'


def test_cancelled_batch_export_keeps_all_rounds_and_empty_results(database):
    c = client()
    batch = create(c, rounds=2)
    c.post(f"/api/batches/{batch['id']}/cancel")
    rows = download(c, batch)
    assert len(rows) == 180
    assert len({r['call_id'] for r in rows}) == 180
    assert {r['batch_status'] for r in rows} == {'cancelled'}
    assert {r['call_status'] for r in rows} == {'cancelled'}
    assert rows[3]['round_index'] == '2' and rows[6]['source_task_id'] == 'HumanEval/1'
    assert all(r['initial_verdict'] == r['agent_total_tokens'] == r['final_response_text'] == '' for r in rows)


def test_export_requires_auth_and_existing_batch(database):
    path = f'/api/batches/{uuid4()}/results.csv'
    assert TestClient(app).get(path).status_code == 401
    assert client().get(path).status_code == 404
    assert client().get('/api/batches/not-an-id/results.csv').status_code == 422
