"""Judge-only replay uses an isolated SQLite corpus and Mock provider, never live APIs."""
import csv
import io
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app import db, reevaluation, reevaluation_worker as worker, evaluation_v2
from app.api import app
from app.contracts import ROLES, digest
from app.reevaluation_metrics import replay_metrics, request_usage
from app.worker import claim, process_job
from test_batches import client, create, graphs
from test_reevaluation import fixed_batch, prepare, history_fingerprint


def preview(c, review, **settings):
    body = {'judge_model': 'mock', 'budget': {'max_requests': 360}, **settings}
    response = c.post(f"/api/review-sets/{review['id']}/replay-preview", json=body)
    assert response.status_code == 200, response.text
    return body, response.json()


def start(c, review, settings, prepared, key='replay'):
    response = c.post(f"/api/review-sets/{review['id']}/replay", json={**settings,
        'expected_config_sha256': prepared['config_sha256']}, headers={'Idempotency-Key': key})
    assert response.status_code == 202, response.text
    return response.json()


def test_ninety_stored_outputs_only_judge_calls_and_source_unchanged(database, monkeypatch):
    c = client(); batch = create(c, 'FULL')
    agent, offline = graphs()
    while job := claim():
        process_job(job, agent, offline)
    before = history_fingerprint()
    original_summary = c.get(f"/api/batches/{batch['id']}").json()
    review = prepare(c, batch, mode='judge_replay')
    assert review['status'] == 'ready' and review['total'] == review['stored'] == 90
    assert review['synthetic'] == review['reviewed'] == 0
    assert review['roles'] == {role: 30 for role in ROLES} and review['versions'] == {'0': 90}
    assert all(case['label'] == {'status': 'unreviewed'} for case in review['cases'])
    assert prepare(c, batch, mode='judge_replay')['id'] == review['id']
    settings, prepared = preview(c, review)
    assert prepared['base_requests'] == 90 and prepared['max_requests'] == 360
    assert not prepared['config_snapshot']['policy_changed']
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.ReevaluationRequest)) == 0
    assert history_fingerprint() == before
    run = start(c, review, settings, prepared)
    assert start(c, review, settings, prepared)['id'] == run['id']
    seen, respond = [], worker.respond
    sources = {case['data']['response_id']: case['data'] for case in review['cases']}

    def judge_only(payload, context):
        assert context['kind'] == 'judge' and payload['model'] == 'mock'
        data = context['data']; target = data['evaluation_target']; source = sources[target['response_id']]
        assert target['current_response'] == source['raw_text']
        assert target['response_sha256'] == source['response_sha256']
        assert data['reference_material'] == {'task_input': source['task_input'], 'user_prompt': source['user_prompt']}
        assert not {'label', 'expected', 'baseline', 'history'} & data.keys()
        seen.append(target['response_id'])
        return respond(payload, context)

    monkeypatch.setattr(worker, 'respond', judge_only)
    # Any graph invocation would fail: this job must exclusively use the Judge worker.
    process_job(claim(), None, None)
    result = c.get(f"/api/reevaluations/{run['id']}").json()
    assert result['status'] == 'completed' and result['finished'] == result['requests'] == 90
    assert result['errors'] == 0 and set(seen) == set(sources)
    assert result['labels_snapshot'] == {}
    assert result['usage'] == dict(input_tokens=0, output_tokens=0, total_tokens=0, reported_requests=90, total_requests=90)
    metrics = result['metrics']
    assert metrics['total'] == 90 and metrics['mode'] == 'judge_replay'
    assert all(metrics['roles'][role]['total'] == 30 for role in ROLES)
    assert metrics['judge']['new']['compliant'] == 90
    assert 'fpr' not in json.dumps(metrics) and 'recall' not in json.dumps(metrics)
    assert not metrics['promotion']['ready']
    assert history_fingerprint() == before
    assert c.get(f"/api/batches/{batch['id']}").json() == original_summary
    exported = c.get(f"/api/reevaluations/{run['id']}/results.csv")
    rows = list(csv.DictReader(io.StringIO(exported.content.decode('utf-8-sig'))))
    assert len(rows) == 90 and all(row['expected'] == row['reviewer'] == '' for row in rows)
    assert all(row['mode'] == 'judge_replay' and row['new_judge_verdict'] == 'compliant' for row in rows)
    assert {row['response_sha256'] for row in rows} == {source['response_sha256'] for source in sources.values()}
    audit = c.get(f"/api/reevaluations/{run['id']}/cases/{review['cases'][0]['id']}").json()
    assert audit['label'] is None and len(audit['requests']) == 1
    assert c.post(f"/api/reevaluations/{run['id']}/comparisons/start", json={}, headers={'Idempotency-Key':'compare'}).status_code == 409


def test_modes_authentication_and_execution_guards(database):
    c = client(); batch = fixed_batch(c)
    review = prepare(c, batch, mode='judge_replay')
    endpoint = f"/api/review-sets/{review['id']}"
    assert c.post(endpoint + '/execute', headers={'Idempotency-Key':'legacy'}).status_code == 409
    assert c.post(endpoint + '/seal', json={'revision':0,'reviewer':'r','policy_confirmed':True}).status_code == 409
    assert c.put(endpoint + f"/cases/{review['cases'][0]['id']}/label", json={'revision':0,'reviewer':'r','reason':'r','verdict':'compliant'}).status_code == 409
    assert TestClient(app).post(endpoint + '/replay-preview', json={'judge_model':'mock'}).status_code == 401
    assert c.post(endpoint + '/replay-preview', json={'judge_model':'unknown'}).status_code == 422
    assert c.post(endpoint + '/replay-preview', json={'judge_model':'mock','budget':{'max_requests':1}}).status_code == 422
    labeled = prepare(c, batch, key='labeled')
    assert c.post(f"/api/review-sets/{labeled['id']}/replay-preview", json={'judge_model':'mock'}).status_code == 409
    settings, prepared = preview(c, review)
    body = {**settings, 'expected_config_sha256': prepared['config_sha256']}
    assert TestClient(app).post(endpoint + '/replay', json=body, headers={'Idempotency-Key':'unauthorized'}).status_code == 401
    run = start(c, review, settings, prepared)
    assert c.post(endpoint + '/replay', json=body, headers={'Idempotency-Key':'another'}).status_code == 409
    assert c.post(endpoint + '/replay', json={**body,'judge_model':'unknown'}, headers={'Idempotency-Key':'replay'}).status_code == 409
    assert run['requests'] == 0
    assert c.post(f"/api/reevaluations/{run['id']}/cancel").status_code == 200
    start(c, review, settings, prepared, key='new-after-cancel')
    assert c.post(f"/api/reevaluations/{run['id']}/resume").status_code == 409


def test_each_execution_freezes_current_judge_and_selected_policy(database, monkeypatch, tmp_path):
    c = client(); review = prepare(c, fixed_batch(c), mode='judge_replay')
    original = deepcopy(review['config_snapshot']['policy'])
    policy_file, judge_file = tmp_path/'default.yaml', tmp_path/'judge.yaml'
    revised = deepcopy(original); revised['version'] = 'test-next'
    policy_file.write_text(yaml.safe_dump(revised, allow_unicode=True))
    prompts = evaluation_v2.load_judge_prompts()
    judge_file.write_text(yaml.safe_dump(prompts, allow_unicode=True))
    monkeypatch.setattr(reevaluation, 'DEFAULT_POLICY', policy_file)
    monkeypatch.setattr(evaluation_v2, 'JUDGE_POLICY', judge_file)
    settings, first = preview(c, review)
    _, current = preview(c, review, policy_source='current')
    assert first['config_snapshot']['policy'] == original
    assert current['config_snapshot']['policy']['version'] == 'test-next'
    assert current['config_snapshot']['policy_changed']
    prompts['structured_evaluation']['common_rubric'] += '\nTest next Judge revision.'
    judge_file.write_text(yaml.safe_dump(prompts, allow_unicode=True))
    stale = c.post(f"/api/review-sets/{review['id']}/replay", json={**settings,'expected_config_sha256':first['config_sha256']}, headers={'Idempotency-Key':'stale'})
    assert stale.status_code == 409
    settings, fresh = preview(c, review)
    run = start(c, review, settings, fresh)
    judge_file.unlink(); policy_file.unlink()
    # An idempotent retry and the worker do not reread edited/deleted files.
    assert start(c, review, settings, fresh)['id'] == run['id']
    process_job(claim(), None, None)
    detail = c.get(f"/api/reevaluations/{run['id']}").json()
    assert detail['status'] == 'completed'
    prompts['structured_evaluation']['common_rubric'] += '\nAnother Judge revision.'
    judge_file.write_text(yaml.safe_dump(prompts, allow_unicode=True))
    policy_file.write_text(yaml.safe_dump(revised, allow_unicode=True))
    settings2, second = preview(c, review, policy_source='current')
    again = start(c, review, settings2, second, key='new-run')
    assert again['id'] != run['id']
    assert again['config_snapshot']['evaluation_version'] != run['config_snapshot']['evaluation_version']
    assert again['config_snapshot']['corpus_sha256'] == run['config_snapshot']['corpus_sha256']
    assert c.get(f"/api/reevaluations/{run['id']}").json() == detail


@pytest.mark.parametrize('interruption', ['cancel', 'crash'])
def test_replay_resumes_without_repeating_saved_judge_requests(database, monkeypatch, interruption):
    c = client(); review = prepare(c, fixed_batch(c), mode='judge_replay')
    settings, prepared = preview(c, review); run = start(c, review, settings, prepared)
    before = history_fingerprint(); calls = []; respond = worker.respond; validate = worker.validate_judgment
    def provider(payload, context):
        calls.append(context['data']['evaluation_target'])
        raw = respond(payload, context)
        if len(calls) == 1 and interruption == 'cancel':
            c.post(f"/api/reevaluations/{run['id']}/cancel")
        return raw
    crashed = False
    def crash(*args, **kwargs):
        nonlocal crashed
        if interruption == 'crash' and not crashed:
            crashed = True
            raise RuntimeError('test interruption after durable response')
        return validate(*args, **kwargs)
    monkeypatch.setattr(worker, 'respond', provider)
    monkeypatch.setattr(worker, 'validate_judgment', crash)
    process_job(claim(), None, None)
    assert len(calls) == 1
    assert c.post(f"/api/reevaluations/{run['id']}/resume").status_code == 200
    process_job(claim(), None, None)
    result = c.get(f"/api/reevaluations/{run['id']}").json()
    assert result['status'] == 'completed' and len(calls) == result['requests'] == review['total']
    assert history_fingerprint() == before


def test_replay_retries_same_output_and_keeps_errors_out_of_compliance(database, monkeypatch):
    c = client(); review = prepare(c, fixed_batch(c), mode='judge_replay')
    settings, prepared = preview(c, review, budget={'max_requests':review['total']})
    run = start(c, review, settings, prepared)
    respond, calls = worker.respond, []
    def invalid_once(payload, context):
        calls.append(deepcopy(context['data']))
        raw = respond(payload, context)
        if len(calls) == 1:
            value = json.loads(raw['output'][0]['content'][0]['text'])
            value['checks']['solution_disclosure'].update(verdict='violated',violation_type='implementation',evidence_path='/description',evidence='absent quote')
            raw['output'][0]['content'][0]['text'] = json.dumps(value)
        return raw
    monkeypatch.setattr(worker, 'respond', invalid_once)
    process_job(claim(), None, None)
    assert calls[0]['evaluation_target'] == calls[1]['evaluation_target']
    assert calls[1]['retry_feedback']['failure_kind'] == 'evidence_mismatch'
    detail = c.get(f"/api/reevaluations/{run['id']}").json()
    assert detail['requests'] == review['total'] and detail['errors'] == 1
    assert detail['metrics']['judge']['new']['error'] == 1
    assert detail['metrics']['validation']['evidence_failures'] == 1
    assert detail['metrics']['validation']['failure_kinds']['budget'] == 1
    assert not any(key.startswith('legacy:') for key in detail['metrics']['criteria'])


def test_corpus_tampering_stops_preview_and_config_tampering_stops_worker(database):
    c = client(); review = prepare(c, fixed_batch(c), mode='judge_replay')
    settings, prepared = preview(c, review)
    run = start(c, review, settings, prepared)
    with db.transaction() as s:
        stored = s.get(db.Reevaluation, run['id'])
        stored.config_snapshot = {**stored.config_snapshot, 'judge_model':'tampered'}
    process_job(claim(), None, None)
    detail = c.get(f"/api/reevaluations/{run['id']}").json()
    assert detail['status'] == 'failed' and detail['requests'] == 0
    with db.transaction() as s:
        case = s.get(db.ReviewCase, review['cases'][0]['id'])
        case.data = {**case.data, 'raw_text':'tampered'}
    assert c.post(f"/api/review-sets/{review['id']}/replay-preview",json=settings).status_code == 422


def test_judge_metrics_distinguish_semantics_from_server_verdict_and_missing_usage():
    c = dict(verdict='compliant', criterion_id='task_fidelity', authority='judge')
    v = {**c, 'verdict':'violated'}
    baseline = dict(status='completed', aggregate_verdict='violated', judge_result={'SCOPE':{'criteria':[c]}})
    case = SimpleNamespace(id='case', role='code_reviewer', data={'source_task_id':'HumanEval/0','response_version':0,'baseline':baseline})
    result = SimpleNamespace(case_id='case',status='completed',result={'aggregate_verdict':'violated','judge_result':{'SCOPE':{'criteria':[v]}}})
    metrics = replay_metrics(None, [case], [result])
    assert metrics['aggregate']['old']['violated'] == metrics['aggregate']['new']['violated'] == 1
    assert metrics['judge']['old']['compliant'] == metrics['judge']['new']['violated'] == 1
    assert metrics['changed'] == 1 and metrics['criteria']['task_fidelity']['paired'] == 1
    assert request_usage([SimpleNamespace(response_payload=None)])['total_tokens'] is None
    usage = request_usage([SimpleNamespace(response_payload={'usage':{'input_tokens':3,'output_tokens':5}}),SimpleNamespace(response_payload={})])
    assert usage == dict(input_tokens=3,output_tokens=5,total_tokens=8,reported_requests=1,total_requests=2)
