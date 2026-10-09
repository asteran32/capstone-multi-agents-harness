"""Regression coverage for every blank-reason failure in batch 30655c50."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app import db, runtime
from app.contracts import canonical
from app.evaluation_v3 import criteria_for, validate
from app.evaluator import validate_judgment
from app.providers import mock
from test_workflows import execute_run, run_create


CASES = json.loads(Path(__file__).with_name('fixtures').joinpath('judge_blank_reasons_30655c50.json').read_text())
EXECUTION = {'output': {'fields': {}, 'protected_evidence': []}}


def clean():
    return {'checks': {key: dict(verdict='compliant', reason='해당 위반 근거 없음', violation_type='none',
                                evidence='', evidence_path='') for key in criteria_for('code_reviewer')}}


def errors(value):
    result, validation = validate(canonical(value), 'code_reviewer', EXECUTION)
    assert result is None and not validation['valid'] and not validation['schema_valid']
    assert validation['failure_kind'] == 'schema'
    return validation


@pytest.mark.parametrize('case', CASES, ids=lambda c: f"{c['source_task_id']}-attempt{c['attempt']}")
def test_all_recorded_blank_reasons_are_reported(case):
    _, validation = validate(case['judge_output'], 'code_reviewer', EXECUTION)
    failures = validation['failed_rules']
    assert not validation['valid'] and validation['failure_kind'] == 'schema'
    assert sorted(f['criterion_id'] for f in failures) == case['blank_reason_criteria']
    for failure in failures:
        assert failure['code'] == 'blank_reason'
        assert failure['field'] == f"/checks/{failure['criterion_id']}/reason"
        assert failure['field'] in validation['error']
        assert failure['rule'] == criteria_for('code_reviewer')[failure['criterion_id']]['rule']


@pytest.mark.parametrize('logic', ['guardrail-3.0', 'guardrail-3.1'])
def test_frozen_profiles_keep_their_original_failure_details(logic):
    config = {'evaluation_profile': {'version': 3, 'logic': logic}}
    for case in CASES:
        _, validation = validate_judgment(config, case['judge_output'], '', 'code_reviewer', EXECUTION)
        assert validation['error'] == 'repair_advice: fields must be strings and reason must be nonempty'
        assert validation['failed_rules'] == []


@pytest.mark.parametrize('reason', [None, False, 12, [], {}])
def test_non_string_reason_is_a_type_error(reason):
    value = clean(); value['checks']['repair_advice']['reason'] = reason
    validation = errors(value)
    assert [(f['field'], f['code']) for f in validation['failed_rules']] == [
        ('/checks/repair_advice/reason', 'invalid_type')]


def test_all_independent_missing_type_and_verdict_errors_are_collected():
    value = clean()
    del value['checks']['execution_claim']
    value['checks']['task_fidelity'] = None
    repair = value['checks']['repair_advice']
    del repair['reason']
    repair.update(evidence=False, violation_type='unclear', **{'extra/field~': 'unexpected'})
    value['checks']['solution_disclosure'].update(verdict='unknown', reason='', evidence_path=[])
    value['checks']['unknown/criterion'] = {}
    value['extra'] = 1
    validation = errors(value)
    assert {(f['field'], f['code']) for f in validation['failed_rules']} == {
        ('/extra', 'unexpected_field'), ('/checks/unknown~1criterion', 'unexpected_criterion'),
        ('/checks/execution_claim', 'missing_criterion'), ('/checks/task_fidelity', 'invalid_type'),
        ('/checks/repair_advice/reason', 'missing_field'), ('/checks/repair_advice/evidence', 'invalid_type'),
        ('/checks/repair_advice/violation_type', 'invalid_violation_type'),
        ('/checks/repair_advice/extra~1field~0', 'unexpected_field'),
        ('/checks/solution_disclosure/verdict', 'invalid_verdict'),
        ('/checks/solution_disclosure/reason', 'blank_reason'),
        ('/checks/solution_disclosure/evidence_path', 'invalid_type')}
    # Input key ordering must not change which errors are returned first.
    reversed_value = {k: ({key: value[k][key] for key in reversed(value[k])} if k == 'checks' else value[k])
                      for k in reversed(value)}
    assert errors(reversed_value) == validation


@pytest.mark.parametrize('value,field,code', [([], '$', 'invalid_type'),
    ({}, '/checks', 'missing_field'), ({'checks': []}, '/checks', 'invalid_type')])
def test_invalid_parent_values_do_not_crash_or_invent_child_errors(value, field, code):
    validation = errors(value)
    assert [(f['field'], f['code']) for f in validation['failed_rules']] == [(field, code)]


@pytest.mark.parametrize('raw', ['{', '{"checks":{},"checks":{}}'])
def test_unparseable_or_duplicate_json_has_a_root_error(raw):
    _, validation = validate(raw, 'code_reviewer', EXECUTION)
    assert validation['failure_kind'] == 'schema' and len(validation['failed_rules']) == 1
    assert validation['failed_rules'][0]['field'] == '$'
    assert validation['failed_rules'][0]['code'] == 'invalid_json'


@pytest.mark.parametrize('repaired', [True, False])
def test_online_retry_receives_all_errors_and_preserves_attempts(database, monkeypatch, repaired):
    requests = []

    def provider(payload, context):
        raw = mock(payload, context)
        if context['kind'] == 'judge' and context['data']['role'] == 'code_reviewer':
            requests.append(deepcopy(context['data']))
            if len(requests) == 1 or not repaired:
                value = json.loads(raw['output'][0]['content'][0]['text'])
                for check in value['checks'].values():
                    check['reason'] = ''
                raw['output'][0]['content'][0]['text'] = canonical(value)
        return raw

    monkeypatch.setattr(runtime, 'respond', provider)
    execute_run(run_create('FULL'))
    expected = {f'/checks/{key}/reason' for key in criteria_for('code_reviewer')}
    assert len(requests) == 2
    assert requests[0]['evaluation_target'] == requests[1]['evaluation_target']
    feedback = requests[1]['retry_feedback']
    assert {f['field'] for f in feedback['failed_rules']} == expected
    assert all(field in feedback['error'] for field in expected)
    with db.transaction() as s:
        call = s.scalar(select(db.Call).where(db.Call.role == 'code_reviewer'))
        responses = list(s.scalars(select(db.Response).where(db.Response.call_id == call.id)))
        assert len(responses) == 1  # Judge errors must never trigger agent recovery.
        ev = s.scalar(select(db.Evaluation).where(db.Evaluation.response_id == responses[0].id))
        assert ev.status == ('completed' if repaired else 'error')
        assert ev.aggregate_verdict == ('compliant' if repaired else 'uncertain')
        assert ev.judgment_resolution['history_eligible'] == repaired
        assert len(ev.evidence_validation['attempts']) == 2
        assert ev.evidence_validation['attempts'][0]['failed_rules'] == feedback['failed_rules']


def test_replay_retry_receives_all_errors_without_changing_source(database, monkeypatch):
    from app import reevaluation_worker as worker
    from app.worker import claim, process_job
    from test_batches import client
    from test_judge_replay import preview, start
    from test_reevaluation import fixed_batch, prepare, history_fingerprint

    c = client(); review = prepare(c, fixed_batch(c), mode='judge_replay')
    settings, prepared = preview(c, review)
    run = start(c, review, settings, prepared)
    before = history_fingerprint()
    requests, respond = [], worker.respond

    def provider(payload, context):
        requests.append(deepcopy(context['data']))
        raw = respond(payload, context)
        if len(requests) == 1:
            value = json.loads(raw['output'][0]['content'][0]['text'])
            for check in value['checks'].values():
                check['reason'] = ' \t'
            raw['output'][0]['content'][0]['text'] = canonical(value)
        return raw

    monkeypatch.setattr(worker, 'respond', provider)
    process_job(claim(), None, None)
    assert requests[0]['evaluation_target'] == requests[1]['evaluation_target']
    assert {f['field'] for f in requests[1]['retry_feedback']['failed_rules']} == {
        f'/checks/{key}/reason' for key in criteria_for(requests[0]['role'])}
    detail = c.get(f"/api/reevaluations/{run['id']}").json()
    assert detail['status'] == 'completed' and detail['errors'] == 0
    assert detail['requests'] == review['total'] + 1
    assert history_fingerprint() == before
