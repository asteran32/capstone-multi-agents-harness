"""Partial human labels do not become a fabricated full-corpus ground truth."""
import csv
import io
from copy import deepcopy
import pytest
from sqlalchemy import select

from app import db
from app.contracts import digest
from app.reevaluation_metrics import validation_metrics
from app.worker import claim, process_job
from test_batches import client
from test_reevaluation import fixed_batch, prepare, execute, history_fingerprint, metric_fixture


def test_one_selected_review_evaluates_only_selection_and_retains_existing_judgments(database):
    c = client(); batch = fixed_batch(c)
    before = history_fingerprint()
    review = prepare(c, batch)
    initial_labels = {case['id']: deepcopy(case['label']) for case in review['cases']}
    selected = next(case for case in review['cases'] if case['kind'] == 'stored'
                    and case['role'] == 'code_reviewer' and case['data']['response_version'] == 1)
    prefix = f"/api/review-sets/{review['id']}"
    result = c.put(f"{prefix}/cases/{selected['id']}/label", json={
        'revision': review['revision'], 'verdict': 'violated', 'reviewer': 'selected fixture reviewer',
        'reason': 'Explicit test label for only this response; not a production judgment.'})
    assert result.status_code == 200, result.text
    partial = result.json()
    assert partial['reviewed'] == 1 and partial['total'] == 17
    assert all(case['label'] == initial_labels[case['id']] for case in partial['cases'] if case['id'] != selected['id'])
    sealed = c.post(prefix + '/seal', json={'revision': partial['revision'], 'reviewer': 'r', 'policy_confirmed': True})
    assert sealed.status_code == 200, sealed.text
    sealed = sealed.json()
    assert sealed['status'] == 'ready'
    assert sealed['review']['reviewed_case_ids'] == [selected['id']]
    assert sealed['review']['reviewed_count'] == 1 and sealed['review']['unreviewed_count'] == 16
    assert sealed['review']['labels_sha256'] == digest({case['id']: case['label'] for case in sealed['cases']})
    run = execute(c, sealed)
    assert run['evaluation_total'] == 1 and run['evaluation_finished'] == 0
    assert run['retained'] == 4 and run['skipped'] == 12
    assert run['config_snapshot']['selected_case_ids'] == [selected['id']]
    # Cancel/restart must not widen the selected scope or call the retained cases.
    assert c.post(f"/api/reevaluations/{run['id']}/cancel").status_code == 200
    assert c.post(f"/api/reevaluations/{run['id']}/resume").status_code == 200
    process_job(claim(), None, None)
    detail = c.get(f"/api/reevaluations/{run['id']}").json()
    assert detail['status'] == 'completed' and detail['total'] == detail['finished'] == 17
    assert detail['requests'] == detail['evaluation_total'] == detail['evaluation_finished'] == 1
    assert detail['retained'] == 4 and detail['skipped'] == 12
    by_case = {row['case_id']: row for row in detail['results']}
    for case in sealed['cases']:
        audit = c.get(f"/api/reevaluations/{run['id']}/cases/{case['id']}").json()
        row = by_case[case['id']]
        if case['id'] == selected['id']:
            assert len(audit['requests']) == 1 and row['status'] == 'completed'
        else:
            assert audit['requests'] == []
            baseline = case['data']['baseline']
            assert row['status'] == ('retained' if baseline is not None else 'skipped')
            assert row['result'] == (baseline if baseline is not None else {})
    assert sum(label['status'] == 'reviewed' for label in detail['labels_snapshot'].values()) == 1
    metrics = detail['metrics']
    assert metrics['reviewed'] == metrics['paired']['total'] == 1
    assert metrics['unreviewed'] == 16 and metrics['total'] == 17
    assert metrics['stored']['new']['total'] == 1 and metrics['synthetic']['new']['total'] == 0
    assert sum(row['count'] for row in metrics['transitions']) == 5  # All stored outputs, not just labeled one.
    assert metrics['judge']['attempts'] == metrics['judge']['schema_checks'] == 1
    assert metrics['retained'] == 4 and metrics['skipped'] == 12
    assert metrics['applied']['total'] == 5 and sum(metrics['applied']['verdicts'].values()) == 5
    assert not metrics['promotion']['ready']
    rows = list(csv.DictReader(io.StringIO(c.get(f"/api/reevaluations/{run['id']}/results.csv").content.decode('utf-8-sig'))))
    assert len(rows) == 17
    assert [row['case_id'] for row in rows if row['label_status'] == 'reviewed'] == [selected['id']]
    untouched = [row for row in rows if row['case_id'] != selected['id']]
    assert all(row['label_status'] == initial_labels[row['case_id']]['status']
               and row['expected'] == row['reviewer'] == row['reason'] == '' for row in untouched)
    assert all(row['new_verdict'] == row['new_judge_verdict'] == '' for row in untouched)
    assert all(row['applied_verdict'] == row['baseline_verdict']
               and row['applied_evaluation_version'] == row['baseline_version']
               for row in untouched if row['result_origin'] == 'retained')
    assert sum(row['result_origin'] == 'retained' for row in rows) == 4
    assert sum(row['result_origin'] == 'skipped' for row in rows) == 12
    assert history_fingerprint() == before


def test_partial_seal_preserves_revision_and_immutable_labels(database):
    c = client(); review = prepare(c, fixed_batch(c))
    prefix = f"/api/review-sets/{review['id']}"
    case = review['cases'][0]
    label = {'revision': review['revision'], 'reviewer': 'r', 'verdict': 'uncertain', 'reason': 'Selected case only'}
    updated = c.put(f"{prefix}/cases/{case['id']}/label", json=label).json()
    assert c.post(prefix + '/seal', json={'revision': review['revision'], 'reviewer': 'r', 'policy_confirmed': True}).status_code == 409
    assert c.post(prefix + '/seal', json={'revision': updated['revision'], 'reviewer': 'r', 'policy_confirmed': True}).status_code == 200
    ready = c.get(prefix).json()
    assert c.put(f"{prefix}/cases/{case['id']}/label", json={**label, 'revision': ready['revision']}).status_code == 409
    assert c.post(prefix + '/seal', json={'revision': ready['revision'], 'reviewer': 'r', 'policy_confirmed': True}).status_code == 200


def test_unreviewed_cases_do_not_inflate_accuracy_denominators_or_promotion():
    run, cases, results = metric_fixture()
    # Even though this baseline/prediction is C, not inspecting it is not human confirmation.
    run.labels_snapshot[cases[0].id] = {'status': 'unreviewed'}
    metrics = validation_metrics(run, cases, results)
    assert metrics['reviewed'] == 3 and metrics['unreviewed'] == 1
    assert metrics['stored']['new']['total'] == metrics['paired']['total'] == 1
    assert metrics['stored']['new']['expected_c'] == 0
    assert metrics['stored']['new']['fpr'] is None
    assert not metrics['promotion']['ready']


@pytest.mark.parametrize('original', ['compliant', 'violated', 'uncertain', 'error', None])
def test_unselected_baseline_is_kept_even_for_error_or_missing_judgment(database, original):
    c = client(); batch = fixed_batch(c)
    with db.transaction() as s:
        baseline = s.scalar(select(db.Evaluation).order_by(db.Evaluation.id))
        response_id = baseline.response_id
        if original is None:
            baseline.phase = 'offline'  # FULL reads online; this response has no applicable baseline.
        else:
            baseline.status = 'error' if original == 'error' else 'completed'
            baseline.aggregate_verdict = 'uncertain' if original == 'error' else original
    before = history_fingerprint()
    review = prepare(c, batch)
    retained = next(case for case in review['cases'] if case['data'].get('response_id') == response_id)
    selected = next(case for case in review['cases'] if case['kind'] == 'synthetic')
    prefix = f"/api/review-sets/{review['id']}"
    partial = c.put(f"{prefix}/cases/{selected['id']}/label", json={
        'revision': review['revision'], 'reviewer': 'r', 'verdict': 'compliant', 'reason': 'Only this synthetic case'}).json()
    sealed = c.post(prefix + '/seal', json={'revision': partial['revision'], 'reviewer': 'r', 'policy_confirmed': True}).json()
    run = execute(c, sealed)
    process_job(claim(), None, None)
    detail = c.get(f"/api/reevaluations/{run['id']}").json()
    assert detail['requests'] == 1 and detail['errors'] == 0
    audit = c.get(f"/api/reevaluations/{run['id']}/cases/{retained['id']}").json()
    assert audit['requests'] == []
    assert audit['result']['result'] == (retained['data']['baseline'] or {})
    assert audit['result']['status'] == ('skipped' if original is None else 'retained')
    metrics = detail['metrics']
    key = original or 'pending'
    assert metrics['applied']['verdicts'][key] >= 1
    assert {'old': key, 'new': key, 'count': metrics['applied']['verdicts'][key]} in metrics['transitions']
    assert metrics['judge']['attempts'] == 1
    assert history_fingerprint() == before


def test_retained_validation_attempts_are_not_new_judge_attempts():
    run, cases, results = metric_fixture()
    run.labels_snapshot[cases[0].id] = {'status': 'unreviewed'}
    results[0].status = 'retained'
    results[0].result = {**cases[0].data['baseline'], 'validation_attempts': [
        {'schema_valid': False, 'evidence_required': True, 'evidence_valid': False, 'valid': False, 'failure_kind': 'schema'}]}
    metrics = validation_metrics(run, cases, results)
    assert metrics['judge']['attempts'] == 0 and metrics['judge']['failure_kinds'] == {}
    assert metrics['retained'] == 1
