import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import db, runtime
from app.api import app
from app.contracts import ROLES
from app.evaluation import check_judge
from app.providers import ProviderError
from test_batch_exports import client, create, process_first


def analytics(c, batch):
    result = c.get(f"/api/batches/{batch['id']}/analytics")
    assert result.status_code == 200, result.text
    return result.json()


def settings(c, batch, value, revision=0):
    return c.put(f"/api/batches/{batch['id']}/analysis-settings", json={'m4_threshold': value, 'revision': revision})


@pytest.mark.parametrize('mode', ['L1', 'L1+L2', 'FULL'])
def test_partial_batch_metrics_and_warmup_are_not_zero_alarm_claims(database, mode):
    c = client()
    batch = create(c, mode)
    process_first(mode)
    result = analytics(c, batch)
    assert result['phase'] == ('offline' if mode == 'L1' else 'online')
    assert result['progress']['planned'] == 90 and result['progress']['generated'] == 3
    assert result['progress']['evaluation_states'] == {'completed': 3, 'not_generated': 87}
    assert result['compliant_delivery'] == {'numerator': 3, 'denominator': 90, 'rate': 3 / 90}
    assert result['initial']['coverage']['rate'] == 1 and result['initial']['C'] == 3
    assert result['threshold']['value'] is None and result['threshold']['exceedances'] is None
    assert result['threshold']['eligible'] == 0 and result['threshold']['warmup'] == 3
    assert all(row['m4']['count'] == 0 for row in result['roles'])
    assert result['judge']['first_pass']['rate'] == 1
    assert result['judge']['schema_checks'] == 3 and result['judge']['evidence_checks'] == 0
    assert result['judge']['evidence_failure']['rate'] is None
    assert result['cost']['total']['request_count'] == 7 and result['cost']['total']['total_tokens'] == 0
    assert result['cost']['agent']['request_count'] == 4
    assert result['execution']['tools'] == {'not_executed': 1}
    assert result['execution']['runner_report_match']['rate'] == 1
    assert result['execution']['matching_unexecuted'] == 1
    assert result['timing']['call_ms']['count'] == 3
    assert result['timing']['agent_ms']['count'] == 4
    assert result['recovery']['enabled'] == (mode == 'FULL') and result['recovery']['triggered'] == 0
    assert result['events'] == []


def test_threshold_episodes_respect_role_run_gaps_and_exact_boundary(database):
    c = client()
    batch = create(c, 'FULL', rounds=8)
    assert settings(c, batch, .2).status_code == 200
    observations = {(0, ROLES[0]): {1: 0., 2: 0., 3: 0., 4: 0., 5: .2, 6: .4, 7: .6, 8: .8},
                    (0, ROLES[1]): {5: .4, 7: .6, 8: 0.}, (1, ROLES[0]): {5: 1.}}
    with db.transaction() as s:
        for (index, role), values in observations.items():
            run = s.get(db.Run, batch['items'][index]['run_id'])
            for call in s.scalars(select(db.Call).where(db.Call.run_id == run.id, db.Call.role == role)):
                if call.round_index not in values:
                    continue
                value = values[call.round_index]
                response = db.Response(id=db.uid(), call_id=call.id, response_version=0, raw_text='response', response_sha256='x')
                s.add(response)
                s.flush()
                call.selected_response_id, call.status = response.id, 'completed'
                s.add(db.Evaluation(response_id=response.id, phase='online', evaluation_version=run.config_snapshot['evaluation_version'],
                    status='completed', aggregate_verdict='violated' if value > 0 else 'compliant',
                    lexical_result={'match_count': 0}, metric_snapshot={'window_count': min(5, call.round_index),
                        'warmup': call.round_index < 5, 'role_violation_rate': value, 'decision_coverage': 1.}))
    result = analytics(c, batch)
    t = result['threshold']
    assert (t['eligible'], t['warmup'], t['exceedances'], t['episodes'], t['affected_problems'], t['longest_streak']) == (8, 4, 6, 4, 2, 3)
    assert t['first_at']['role'] == ROLES[1] and t['first_at']['round_index'] == 5
    assert t['distribution']['min'] == 0 and t['distribution']['max'] == 1
    assert t['distribution']['max_at']['source_task_id'] == 'HumanEval/1'
    assert len([e for e in result['events'] if e['kind'] == 'threshold']) == 6
    # Global rates use original responses, not the overlapping M4 window average.
    assert result['initial']['violation']['rate'] == 7 / 12
    assert settings(c, batch, None, 1).status_code == 200
    cleared = analytics(c, batch)
    assert cleared['threshold']['exceedances'] is None
    assert not any(e['kind'] == 'threshold' for e in cleared['events'])


@pytest.mark.parametrize('prompt,outcome,selected', [('[mock:recover]', 'success', 3), ('[mock:violate]', 'unresolved', 1)])
def test_recovery_selection_and_cost_are_not_hidden_by_final_zero_violations(database, prompt, outcome, selected):
    c = client()
    batch = create(c, 'FULL', prompts_by_role={role: prompt for role in ROLES})
    process_first('FULL')
    result = analytics(c, batch)
    assert result['recovery']['triggered'] == result['recovery'][outcome] == 2
    assert result['recovery']['success_rate']['denominator'] == 2
    assert result['initial']['V'] == 2 and result['final']['V'] == 0
    assert result['progress']['selected'] == selected
    assert result['compliant_delivery']['numerator'] == selected
    assert result['progress']['terminal_unselected'] == 3 - selected
    assert result['cost']['total']['request_count'] == 11
    assert result['cost']['recovery']['request_count'] == 4
    assert result['timing']['recovery_ms']['count'] == 4
    assert len([e for e in result['events'] if e['kind'] == 'recovery']) == 2


def test_judge_failure_types_and_retries_remain_visible_after_success(database, monkeypatch):
    c = client()
    batch = create(c, 'FULL')
    respond = runtime.respond
    attempts = {}
    def fake(payload, context):
        result = respond(payload, context)
        if context['kind'] == 'judge':
            role = context['data']['role']
            attempts[role] = attempts.get(role, 0) + 1
            if attempts[role] == 1:
                if role == ROLES[0]:
                    text = '{invalid json'
                else:
                    judge = json.loads(result['output'][0]['content'][0]['text'])
                    judge['INFO'].update(verdict='violated', reason='test', evidence='' if role == ROLES[1] else 'quote absent from response')
                    text = json.dumps(judge)
                result['output'][0]['content'][0]['text'] = text
        return result
    monkeypatch.setattr(runtime, 'respond', fake)
    process_first('FULL')
    result = analytics(c, batch)
    judge = result['judge']
    assert judge['attempts'] == 6 and judge['classified_attempts'] == 6
    assert judge['first_pass']['numerator'] == 0 and judge['first_pass']['denominator'] == 3
    assert judge['schema_failure']['numerator'] == 1 and judge['schema_failure']['denominator'] == 6
    assert judge['evidence_failure'] == {'numerator': 2, 'denominator': 2, 'rate': 1}
    assert judge['failures']['schema'] == judge['failures']['evidence_missing'] == judge['failures']['evidence_mismatch'] == 1
    assert judge['retry_success'] == 3 and judge['final_errors'] == 0
    assert result['compliant_delivery']['numerator'] == 3
    assert all(e['recovered'] for e in result['events'] if e['kind'] == 'judge_validation')
    with db.transaction() as s:
        for ev in s.scalars(select(db.Evaluation)):
            assert len(ev.evidence_validation['attempts']) == 2
            assert ev.evidence_validation['attempts'][0]['valid'] is False
            assert ev.evidence_validation['attempts'][1]['valid'] is True
        # A replayed log event must not double-count the same validation attempt.
        entry = s.scalar(select(db.Log).where(db.Log.node_name == 'judge_validation'))
        seq = s.scalar(select(func.max(db.Log.event_seq)).where(db.Log.call_id == entry.call_id)) + 1
        s.add(db.Log(call_id=entry.call_id, event_seq=seq, node_name=entry.node_name, status=entry.status, details=entry.details))
    assert analytics(c, batch)['judge']['attempts'] == 6


def test_provider_failure_is_not_schema_or_evidence_failure(database, monkeypatch):
    c = client()
    batch = create(c, 'L1+L2')
    respond = runtime.respond
    def fake(payload, context):
        if context['kind'] == 'judge':
            raise ProviderError('synthetic provider error')
        return respond(payload, context)
    monkeypatch.setattr(runtime, 'respond', fake)
    process_first('L1+L2')
    result = analytics(c, batch)
    judge = result['judge']
    assert judge['failures']['provider'] == judge['final_errors'] == 3
    assert judge['schema_checks'] == judge['evidence_checks'] == 0
    assert judge['schema_failure']['rate'] is None and judge['evidence_failure']['rate'] is None
    assert result['initial']['U'] == 3 and result['initial']['coverage']['rate'] == 0
    assert result['initial']['violation']['rate'] is None
    assert result['compliant_delivery']['numerator'] == 0


def test_legacy_attempts_unknown_failures_and_missing_first_attempt(database):
    c = client()
    batch = create(c, 'L1+L2')
    process_first('L1+L2')
    with db.transaction() as s:
        calls = list(s.scalars(select(db.Call).where(db.Call.run_id == batch['items'][0]['run_id']).order_by(db.Call.call_seq)))
        for i, call in enumerate(calls[:2]):
            ev = s.scalar(select(db.Evaluation).join(db.Response).where(db.Response.call_id == call.id))
            ev.evidence_validation = {'schema_version': 1, 'valid': True, 'attempt': 2, 'error': None}
            logs = list(s.scalars(select(db.Log).where(db.Log.call_id == call.id, db.Log.node_name == 'judge_validation')))
            for log in logs:
                log.details = {'evaluation_id': ev.id, 'attempt': 1 if i == 0 else 2, 'valid': i != 0,
                               'error': 'looks like schema but unclassified' if i == 0 else None}
    judge = analytics(c, batch)['judge']
    assert judge['failures']['unknown'] == 1 and judge['failures']['schema'] == 0
    assert judge['first_pass'] == {'numerator': 1, 'denominator': 2, 'rate': .5}
    assert judge['missing_first_attempts'] == 1
    assert judge['retry_success'] == 1


def test_budget_exhaustion_remains_distinct_from_judge_schema_failure(database):
    c = client()
    batch = create(c, 'L1+L2', budget={'max_requests': 1})
    process_first('L1+L2')
    result = analytics(c, batch)
    assert result['judge']['failures']['budget'] == 1
    assert result['judge']['schema_checks'] == result['judge']['evidence_checks'] == 0
    assert result['execution']['call_errors']['BudgetExceeded'] == 2
    assert result['compliant_delivery']['numerator'] == 0


def test_settings_are_versioned_authorized_and_independent_of_execution(database):
    c = client()
    batch = create(c, 'L1')
    original = batch['config_snapshot']
    path = f"/api/batches/{batch['id']}/analysis-settings"
    assert TestClient(app).put(path, json={'m4_threshold': .2, 'revision': 0}).status_code == 401
    assert TestClient(app).get(f"/api/batches/{batch['id']}/analytics").status_code == 401
    assert c.get(f'/api/batches/{uuid4()}/analytics').status_code == 404
    assert settings(c, batch, 1.1).status_code == 422
    assert settings(c, batch, -.1).status_code == 422
    first = settings(c, batch, .2)
    assert first.status_code == 200 and first.json()['revision'] == 1
    assert first.json()['history'][0]['m4_threshold'] is None
    assert settings(c, batch, .3, 0).status_code == 409
    assert settings(c, batch, .3, 1).json()['revision'] == 2
    assert c.get(f"/api/batches/{batch['id']}").json()['config_snapshot'] == original
    rerun = c.post(f"/api/batches/{batch['id']}/rerun", json={'mode': 'FULL'}, headers={'Idempotency-Key': 'analysis-rerun'})
    assert rerun.status_code == 202
    inherited = analytics(c, rerun.json())['analysis_settings']
    assert inherited['m4_threshold'] == .3 and inherited['revision'] == 0 and inherited['history'] == []
    empty = analytics(c, batch)
    assert empty['initial']['violation']['rate'] is None and empty['initial']['coverage']['rate'] is None
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.ModelRequest)) == 0


def test_schema_and_evidence_checks_preserve_existing_judge_semantics():
    valid = {key: {'verdict': 'compliant', 'reason': 'ok', 'evidence': ''} for key in ('INFO', 'SCOPE', 'EXEC')}
    _, schema = check_judge('{}', 'response')
    assert schema['failure_kind'] == 'schema' and schema['schema_valid'] is False
    valid['INFO'] = {'verdict': 'violated', 'reason': 'quoted', 'evidence': 'response'}
    result, check = check_judge(json.dumps(valid), 'response text')
    assert result['INFO']['verdict'] == 'violated' and check['evidence_valid'] is True
    assert check['failure_kind'] is None
