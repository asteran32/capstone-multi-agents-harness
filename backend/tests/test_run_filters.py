from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import app
from app.config import settings


def test_run_model_filter_counts_pages_and_combined_filters(database, monkeypatch):
    monkeypatch.setitem(settings.__dict__, 'live_models', ('live-agent', 'live-judge'))
    client = TestClient(app)
    client.auth = ('researcher', 'test-password')
    task = client.get('/api/tasks').json()[0]
    candidate = client.get(f"/api/tasks/{task['id']}/candidates").json()[0]
    # Include mixed models, failed/queued runs, and a Judge not yet used in Base.
    cases = [
        ('mock', 'mock', 'Base', 'completed', 'mock'),
        ('live-agent', 'mock', 'Base', 'failed', 'live'),
        ('mock', 'live-judge', 'L1+L2', 'completed', 'live'),
        ('live-agent', 'live-judge', 'FULL', 'running', 'live'),
        ('mock', 'mock', 'FULL', 'completed', 'mock'),
        ('mock', 'live-judge', 'Base', 'queued', 'live'),
    ]
    ids = []
    for day, (agent, judge, mode, status, _) in enumerate(cases, start=1):
        result = client.post('/api/runs', json={
            'task_id': task['id'], 'candidate_id': candidate['id'], 'mode': mode,
            'agent_model': agent, 'judge_model': judge,
            'prompts_by_role': {'problem_designer': 'describe', 'code_reviewer': 'review', 'test_runner': 'run'},
        }, headers={'Idempotency-Key': f'filter-{day}'})
        assert result.status_code == 202
        run_id = result.json()['id']
        ids.append(run_id)
        with db.transaction() as s:
            run = s.get(db.Run, run_id)
            run.created_at = datetime(2026, 1, day, tzinfo=timezone.utc)
            run.status = status

    def check(params, expected_ids, total):
        result = client.get('/api/runs', params=params)
        assert result.status_code == 200
        data = result.json()
        assert data['total'] == total
        assert [item['id'] for item in data['items']] == expected_ids
        for item in data['items']:
            agent, judge, _, _, expected_type = cases[ids.index(item['id'])]
            assert item['model_type'] == expected_type
            assert item['config_snapshot']['agent_model'] == agent
            assert item['config_snapshot']['judge_model'] == judge
            assert client.get(f"/api/runs/{item['id']}").json()['model_type'] == expected_type

    check({}, ids[::-1], 6)
    check({'model_type': 'mock'}, [ids[4], ids[0]], 2)
    check({'model_type': 'live'}, [ids[5], ids[3], ids[2], ids[1]], 4)
    check({'model_type': 'live', 'limit': 2}, [ids[5], ids[3]], 4)
    check({'model_type': 'live', 'limit': 2, 'offset': 2}, [ids[2], ids[1]], 4)
    check({'model_type': 'live', 'offset': 4}, [], 4)
    check({'model_type': 'live', 'mode': 'Base', 'status': 'failed',
           'since': '2026-01-02T00:00:00Z', 'until': '2026-01-03T00:00:00Z'}, [ids[1]], 1)
    check({'model_type': 'mock', 'status': 'failed'}, [], 0)
    check({'model_type': 'mock', 'since': '2026-01-06T00:00:00Z'}, [], 0)


@pytest.mark.parametrize('model_type', ['hybrid', 'Mock'])
def test_run_model_filter_rejects_unknown_type(database, model_type):
    client = TestClient(app)
    client.auth = ('researcher', 'test-password')
    assert client.get('/api/runs', params={'model_type': model_type}).status_code == 422
