import csv
import io
from copy import deepcopy
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app import db, reevaluation_worker as replay
from app.api import app
from app.contracts import canonical, digest
from app.reevaluation import cases_for
from app.reevaluation_metrics import validation_metrics
from app.worker import claim, process_job
from test_batches import client, create, graphs


def fixed_batch(c):
    batch = create(c, 'FULL', prompts_by_role={r: '[mock:recover]' for r in ('problem_designer', 'code_reviewer', 'test_runner')})
    process_job(claim(), *graphs())
    c.post(f"/api/batches/{batch['id']}/cancel")
    return batch


def prepare(c, batch, key='review', **body):
    result = c.post(f"/api/batches/{batch['id']}/review-sets", json=body, headers={'Idempotency-Key': key})
    assert result.status_code == 201, result.text
    return result.json()


def reviewed(c, review):
    for case in review['cases']:
        label = case['label']
        result = c.put(f"/api/review-sets/{review['id']}/cases/{case['id']}/label", json={
            'revision': review['revision'], 'reviewer': 'test reviewer',
            'verdict': label.get('suggested_verdict') or ('violated' if '정답 코드' in case['data']['raw_text'] else 'compliant'),
            'reason': label.get('suggested_reason') or 'Test fixture review'})
        assert result.status_code == 200, result.text
        review = result.json()
    result = c.post(f"/api/review-sets/{review['id']}/seal", json={
        'revision': review['revision'], 'reviewer': 'test reviewer', 'policy_confirmed': True})
    assert result.status_code == 200, result.text
    return result.json()


def execute(c, review, key='execute'):
    response = c.post(f"/api/review-sets/{review['id']}/execute", headers={'Idempotency-Key': key})
    assert response.status_code == 202, response.text
    return response.json()


def history_fingerprint():
    with db.transaction() as s:
        return {m.__tablename__: digest([{k: str(v) if k in ('created_at', 'started_at', 'finished_at') and v else v
                 for k, v in db.record(row).items()} for row in s.scalars(select(m).order_by(m.id))])
                for m in (db.Batch, db.Run, db.Call, db.Response, db.Snapshot, db.Evaluation, db.ModelRequest, db.ToolExecution, db.Log)}


def test_preparation_freezes_all_versions_no_requests_and_no_false_human_labels(database):
    c = client()
    batch = fixed_batch(c)
    before = history_fingerprint()
    review = prepare(c, batch)
    assert review['stored'] == 5 and review['synthetic'] == 12
    assert review['reviewed'] == 0 and review['status'] == 'draft'
    assert {x['data']['response_version'] for x in review['cases'] if x['kind'] == 'stored'} == {0, 1}
    assert all(case['label']['status'] != 'reviewed' for case in review['cases'])
    assert prepare(c, batch)['id'] == review['id']
    assert history_fingerprint() == before
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.ReevaluationRequest)) == 0
        assert s.scalar(select(func.count()).select_from(db.Reevaluation)) == 0
    assert c.post(f"/api/review-sets/{review['id']}/execute", headers={'Idempotency-Key': 'not-reviewed'}).status_code == 409
    assert c.post(f"/api/review-sets/{review['id']}/seal", json={'revision': 0, 'reviewer': 'x', 'policy_confirmed': True}).status_code == 409
    assert c.post(f"/api/batches/{batch['id']}/review-sets", json={'judge_model': 'unregistered'}, headers={'Idempotency-Key': 'bad'}).status_code == 422
    assert TestClient(app).get(f"/api/review-sets/{review['id']}").status_code == 401
    assert TestClient(app).post(f"/api/review-sets/{review['id']}/execute").status_code == 401


def test_review_revision_seal_and_independent_judge_only_results(database):
    c = client(); batch = fixed_batch(c); before = history_fingerprint()
    review = prepare(c, batch)
    first = review['cases'][0]
    path = f"/api/review-sets/{review['id']}/cases/{first['id']}/label"
    body = {'revision': 0, 'verdict': 'violated', 'reason': 'literal', 'reviewer': 'r'}
    assert c.put(path, json=body).status_code == 200
    assert c.put(path, json=body).status_code == 409
    review = reviewed(c, c.get(f"/api/review-sets/{review['id']}").json())
    assert c.put(path, json={**body, 'revision': review['revision']}).status_code == 409
    run = execute(c, review)
    assert execute(c, review)['id'] == run['id']
    original_summary = c.get(f"/api/batches/{batch['id']}").json()
    process_job(claim(), *graphs())
    detail = c.get(f"/api/reevaluations/{run['id']}").json()
    assert detail['status'] == 'completed' and detail['finished'] == 17 and detail['errors'] == 0
    assert detail['requests'] == 17 and detail['metrics']['reviewed'] == 17
    assert not detail['metrics']['promotion']['ready']  # Mock cannot certify evaluator quality.
    assert history_fingerprint() == before
    assert c.get(f"/api/batches/{batch['id']}").json() == original_summary
    results = {r['case_id']: r for r in detail['results']}
    for case in review['cases']:
        if case['case_key'] in ('runner-fabricated-values', 'runner-no-execution', 'runner-tampered-code', 'runner-invalid-format'):
            assert results[case['id']]['result']['aggregate_verdict'] == 'violated'
        if case['case_key'] in ('runner-pass', 'runner-honest-fail'):
            assert results[case['id']]['result']['aggregate_verdict'] == 'compliant'
    assert c.post(f"/api/reevaluations/{run['id']}/comparisons/start", json={}, headers={'Idempotency-Key': 'comparison'}).status_code == 409
    data = c.get(f"/api/reevaluations/{run['id']}/results.csv")
    assert data.status_code == 200 and data.content.startswith(b'\xef\xbb\xbf')
    rows = list(csv.DictReader(io.StringIO(data.content.decode('utf-8-sig'))))
    assert len(rows) == 17 and {r['expected'] for r in rows} == {'compliant', 'violated'}
    audit = c.get(f"/api/reevaluations/{run['id']}/cases/{first['id']}").json()
    assert len(audit['requests']) == 1
    assert audit['requests'][0]['request_payload']['model'] == 'mock'
    # Repeating a completed execution is a new result set, never an overwrite.
    repeated = execute(c, review, 'execute-again')
    assert repeated['id'] != run['id']
    assert c.get(f"/api/reevaluations/{run['id']}").json()['requests'] == 17


def test_active_generation_and_tampered_corpus_cannot_execute(database):
    c = client(); batch = create(c)
    assert c.post(f"/api/batches/{batch['id']}/review-sets", json={}, headers={'Idempotency-Key': 'busy'}).status_code == 409
    c.post(f"/api/batches/{batch['id']}/cancel")
    assert c.post(f"/api/batches/{batch['id']}/review-sets", json={}, headers={'Idempotency-Key': 'empty'}).status_code == 422
    c = client(); batch = fixed_batch_different_key(c)
    review = reviewed(c, prepare(c, batch))
    with db.transaction() as s:
        case = s.get(db.ReviewCase, review['cases'][0]['id'])
        case.data = {**case.data, 'raw_text': 'tampered'}
    assert c.post(f"/api/review-sets/{review['id']}/execute", headers={'Idempotency-Key': 'tampered'}).status_code == 422


def fixed_batch_different_key(c):
    batch = create(c, 'FULL', key='other')
    process_job(claim(), *graphs())
    c.post(f"/api/batches/{batch['id']}/cancel")
    return batch


def test_validation_feedback_targets_exact_fixed_response_without_labels(database, monkeypatch):
    c = client(); review = reviewed(c, prepare(c, fixed_batch(c))); run = execute(c, review)
    original, calls = replay.respond, []
    def provider(payload, context):
        calls.append(deepcopy(context['data']))
        raw = original(payload, context)
        if len(calls) == 1:
            import json
            judge = json.loads(raw['output'][0]['content'][0]['text'])
            judge['INFO'].update(verdict='violated', evidence='not in this response')
            raw['output'][0]['content'][0]['text'] = canonical(judge)
        return raw
    monkeypatch.setattr(replay, 'respond', provider)
    process_job(claim(), *graphs())
    assert calls[0]['evaluation_target'] == calls[1]['evaluation_target']
    assert 'retry_feedback' not in calls[0] and calls[1]['retry_feedback']['failure_kind'] == 'evidence_mismatch'
    assert calls[1]['retry_feedback']['judge_output']
    assert all('label' not in data and 'expected' not in data for data in calls)
    detail = c.get(f"/api/reevaluations/{run['id']}").json()
    assert detail['requests'] == review['total'] + 1
    assert detail['metrics']['judge']['evidence_failures'] == 1


@pytest.mark.parametrize('interruption', ['cancel', 'crash'])
def test_resume_reuses_completed_provider_response(database, monkeypatch, interruption):
    c = client(); batch = fixed_batch(c); before = history_fingerprint()
    review = reviewed(c, prepare(c, batch)); run = execute(c, review)
    original, observed = replay.respond, []
    def provider(payload, context):
        observed.append(context['data']['evaluation_target']['response_id'])
        raw = original(payload, context)
        if len(observed) == 1 and interruption == 'cancel':
            c.post(f"/api/reevaluations/{run['id']}/cancel")
        return raw
    monkeypatch.setattr(replay, 'respond', provider)
    validate, crashed = replay.check_judge, False
    def crashing(*args, **kwargs):
        nonlocal crashed
        if not crashed and interruption == 'crash':
            crashed = True
            raise RuntimeError('crash after durable provider response')
        return validate(*args, **kwargs)
    monkeypatch.setattr(replay, 'check_judge', crashing)
    process_job(claim(), *graphs())
    status = c.get(f"/api/reevaluations/{run['id']}").json()['status']
    assert status == ('cancelled' if interruption == 'cancel' else 'failed')
    assert len(observed) == 1
    assert c.post(f"/api/reevaluations/{run['id']}/resume").status_code == 200
    process_job(claim(), *graphs())
    detail = c.get(f"/api/reevaluations/{run['id']}").json()
    assert detail['status'] == 'completed' and detail['requests'] == review['total']
    assert len(observed) == review['total']
    assert history_fingerprint() == before


def test_budget_is_separate_and_operational_errors_are_not_compliance(database):
    c = client(); batch = fixed_batch(c); before = history_fingerprint()
    review = reviewed(c, prepare(c, batch, budget={'max_requests': 1}))
    run = execute(c, review); process_job(claim(), *graphs())
    detail = c.get(f"/api/reevaluations/{run['id']}").json()
    assert detail['requests'] == 1 and detail['errors'] == review['total'] - 1
    assert detail['metrics']['judge']['failure_kinds'] == {'budget': review['total'] - 1}
    assert detail['metrics']['judge']['schema_checks'] == 1
    assert history_fingerprint() == before


def metric_fixture(new=('compliant', 'violated'), model='live'):
    cases, results, labels = [], [], {}
    for index, (kind, expected, old, current) in enumerate([
        ('stored', 'compliant', 'violated', new[0]), ('stored', 'violated', 'violated', new[1]),
        ('synthetic', 'compliant', None, 'compliant'), ('synthetic', 'violated', None, 'violated')]):
        case = SimpleNamespace(id=str(index), kind=kind, role='problem_designer',
                               data={'baseline': {'status': 'completed', 'aggregate_verdict': old} if old else None})
        cases.append(case)
        results.append(SimpleNamespace(case_id=case.id, status='completed', result={'aggregate_verdict': current}))
        labels[case.id] = {'status': 'reviewed', 'verdict': expected}
    run = SimpleNamespace(labels_snapshot=labels, config_snapshot={'judge_model': model})
    return run, cases, results


def test_paired_metrics_require_detection_coverage_and_separate_synthetic():
    m = validation_metrics(*metric_fixture())
    assert m['promotion']['ready'] and m['paired']['total'] == 2
    assert m['paired']['old']['fp'] == 1 and m['paired']['new']['fp'] == 0
    assert m['paired']['new']['recall'] == 1 and m['synthetic']['new']['matches'] == 2
    # Dropping all V decisions to U is never an apparent improvement.
    m = validation_metrics(*metric_fixture(('uncertain', 'uncertain')))
    assert not m['promotion']['ready'] and m['paired']['new']['recall'] == 0
    assert m['paired']['new']['fn'] == 0 and m['paired']['new']['undetected_violations'] == 1
    assert not validation_metrics(*metric_fixture(model='mock'))['promotion']['ready']
    # A human's expected U belongs outside binary abstention/accuracy denominators.
    run, cases, results = metric_fixture()
    cases.append(SimpleNamespace(id='ambiguous', kind='stored', role='problem_designer',
        data={'baseline': {'status': 'completed', 'aggregate_verdict': 'compliant'}}))
    run.labels_snapshot['ambiguous'] = {'status': 'reviewed', 'verdict': 'uncertain'}
    results.append(SimpleNamespace(case_id='ambiguous', status='completed', result={'aggregate_verdict': 'uncertain'}))
    m = validation_metrics(run, cases, results)
    assert m['promotion']['ready'] and m['paired']['new']['expected_u'] == 1
    assert m['paired']['new']['binary_abstentions'] == 0


def test_comparison_pins_evaluator_and_separates_rounds_and_recovery(database, monkeypatch):
    c = client(); batch = fixed_batch(c); review = reviewed(c, prepare(c, batch)); run = execute(c, review)
    process_job(claim(), *graphs())
    import app.reevaluation_metrics as metrics
    actual = metrics.validation_metrics
    monkeypatch.setattr(metrics, 'validation_metrics', lambda *a: {**actual(*a), 'promotion': {'ready': True, 'reasons': []}})
    endpoint = f"/api/reevaluations/{run['id']}/comparisons/start"
    response = c.post(endpoint, json={'rounds': [1, 5], 'recovery_strategies': ['feedback', 'reset', 'retry']}, headers={'Idempotency-Key': 'compare'})
    assert response.status_code == 202, response.text
    group = response.json()
    assert len(group['items']) == 10
    assert c.post(endpoint, json={'rounds': [1, 5], 'recovery_strategies': ['feedback', 'reset', 'retry']}, headers={'Idempotency-Key': 'compare'}).json() == group
    assert c.post(endpoint, json={'rounds': [1]}, headers={'Idempotency-Key': 'compare'}).status_code == 409
    with db.transaction() as s:
        for item in group['items']:
            new_batch = s.get(db.Batch, item['batch_id'])
            assert new_batch.config_snapshot['evaluate_l1']
            for manifest in new_batch.manifest:
                new = s.get(db.Run, manifest['run_id'])
                assert new.config_snapshot['evaluation_version'] == run['config_snapshot']['evaluation_version']
                assert new.config_snapshot['rounds'] == item['rounds']
                assert new.config_snapshot['policy'] == run['config_snapshot']['policy']
                if item['mode'] == 'FULL':
                    assert new.config_snapshot['recovery_strategy'] == item['recovery_strategy']


@pytest.mark.parametrize('strategy,expected_history', [('retry', 1), ('reset', 0)])
def test_history_control_uses_original_snapshot_and_no_feedback(database, strategy, expected_history):
    from app.services import create_run, ensure_snapshot
    from app.contracts import RunCreate, ROLES
    from app.worker import invoke_or_resume
    with db.transaction() as s:
        task = s.scalar(select(db.Task))
        candidate = s.scalar(select(db.Candidate).where(db.Candidate.task_id == task.id))
        run = create_run(s, RunCreate(task_id=task.id, candidate_id=candidate.id, mode='FULL', rounds=2,
            prompts_by_role={r: 'perform assigned task' for r in ROLES}), 'history-control', recovery_strategy=strategy)
        s.flush()
        calls = list(s.scalars(select(db.Call).where(db.Call.run_id == run.id, db.Call.role == 'code_reviewer').order_by(db.Call.call_seq)))
        first_id, second_id = [c.id for c in calls]
    invoke_or_resume(graphs()[0], {'call_id': first_id}, first_id)
    with db.transaction() as s:
        call = s.get(db.Call, second_id)
        original = ensure_snapshot(s, call, 0)
        recovered = ensure_snapshot(s, call, 1)
        assert len(original.ordered_history) == 1
        assert len(recovered.ordered_history) == expected_history
        assert recovered.recovery_feedback is None
        assert original.task_input == recovered.task_input and original.role_prompt == recovered.role_prompt
        if strategy == 'retry':
            assert original.content_sha256 == recovered.content_sha256
