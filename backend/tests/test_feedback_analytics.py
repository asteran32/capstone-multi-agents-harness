import json

from sqlalchemy import select

from app import evaluation_v2, services
from app import db, runtime
from app.contracts import ROLES
from app.providers import mock
from test_batch_exports import client, create, process_first, download
from test_batch_analytics import analytics


def test_conflict_counts_are_evaluations_not_rule_count(database, monkeypatch):
    # Frozen v2 runs retain the original Runner arbitration contract.
    monkeypatch.setattr(services, 'evaluation_profile', lambda policy: evaluation_v2.evaluation_profile())
    c = client()
    batch = create(c, 'FULL')
    def respond(payload, context):
        result = mock(payload, context)
        if context['kind'] == 'judge' and context['data']['role'] == 'test_runner':
            judge = json.loads(result['output'][0]['content'][0]['text'])
            for value in judge.values():
                value.update(verdict='violated', criterion_id='runner_report',
                             evidence=context['data']['evaluation_target']['current_response'])
            result['output'][0]['content'][0]['text'] = json.dumps(judge)
        return result
    monkeypatch.setattr(runtime, 'respond', respond)
    process_first('FULL')
    result = analytics(c, batch)
    assert result['conflicts']['rate'] == {'numerator': 1, 'denominator': 1, 'rate': 1}
    assert result['conflicts']['initial'] == 1 and result['conflicts']['recovery'] == 0
    assert result['recovery']['triggered'] == 0
    events = [e for e in result['events'] if e['kind'] == 'judgment_conflict']
    assert len(events) == 1 and len(events[0]['conflicts']) == 3
    row = download(c, batch)[2]
    assert row['initial_exec_verdict'] == 'compliant'
    assert json.loads(row['initial_judge_result'])['EXEC']['verdict'] == 'violated'
    assert json.loads(row['initial_judgment_resolution'])['conflicts']
    # A historical evaluation has no conflict observation, not a measured zero.
    with db.transaction() as s:
        call = s.scalar(select(db.Call).where(db.Call.run_id == batch['items'][0]['run_id'], db.Call.role == ROLES[2]))
        ev = s.scalar(select(db.Evaluation).join(db.Response).where(db.Response.call_id == call.id))
        ev.judgment_resolution = None
    result = analytics(c, batch)
    assert result['conflicts']['rate']['denominator'] == 0
    assert result['conflicts']['rate']['rate'] is None and result['conflicts']['unmeasured'] == 1


def test_unresolved_and_uncertainty_include_unselected_recovery(database):
    c = client()
    batch = create(c, 'FULL', prompts_by_role={r: '[mock:violate]' for r in ROLES})
    process_first('FULL')
    with db.transaction() as s:
        call = s.scalar(select(db.Call).where(db.Call.run_id == batch['items'][0]['run_id'], db.Call.role == ROLES[0]))
        ev = s.scalar(select(db.Evaluation).join(db.Response).where(db.Response.call_id == call.id, db.Response.response_version == 1))
        ev.status, ev.aggregate_verdict = 'error', 'uncertain'
        ev.judgment_resolution = {'uncertainty_reasons': ['judge_error'], 'conflict_checked': False}
    result = analytics(c, batch)
    assert result['unresolved']['rate'] == {'numerator': 2, 'denominator': 90, 'rate': 2 / 90}
    assert result['unresolved']['primary_causes'] == {'judge_error': 1, 'violation_persisted': 1}
    assert result['uncertainty']['recovery'] == {'evaluations': 1, 'causes': {'judge_error': 1}}
    assert result['uncertainty']['latest']['evaluations'] == 1
    assert result['final']['U'] == 0  # unselected U must remain visible elsewhere
    assert result['compliant_delivery']['numerator'] == 1
    events = [e for e in result['events'] if e['kind'] == 'unresolved']
    assert len(events) == 2 and all(e['response_version'] == 1 for e in events)
    row = download(c, batch)[0]
    assert row['recovery_verdict'] == 'uncertain' and row['final_verdict'] == ''
    assert json.loads(row['recovery_uncertainty_reasons']) == ['judge_error']


def test_recovery_success_types_and_valid_same_response_denominator(database):
    c = client()
    batch = create(c, 'FULL', prompts_by_role={r: '[mock:recover]' for r in ROLES})
    process_first('FULL')
    before = analytics(c, batch)
    assert before['recovery']['success_kinds'] == {'changed_response': 2}
    assert before['recovery']['strategies']['feedback']['success_rate']['rate'] == 1
    with db.transaction() as s:
        calls = list(s.scalars(select(db.Call).where(db.Call.run_id == batch['items'][0]['run_id']).order_by(db.Call.call_seq)))
        for i, call in enumerate(calls[:2]):
            responses = list(s.scalars(select(db.Response).where(db.Response.call_id == call.id).order_by(db.Response.response_version)))
            responses[1].response_sha256 = responses[0].response_sha256
            responses[1].raw_text = responses[0].raw_text
            if i == 1:
                ev = s.scalar(select(db.Evaluation).where(db.Evaluation.response_id == responses[0].id))
                ev.status = 'error'
    result = analytics(c, batch)
    assert result['recovery']['same_response_verdict_change'] == {'numerator': 1, 'denominator': 1, 'rate': 1}
    assert result['recovery']['success_kinds'] == {'same_response_rejudged': 1, 'evaluation_error_resolved': 1}
    assert result['recovery']['comparisons']['error_resolved'] == 1


def test_contract_violation_is_not_mislabeled_report_mismatch(database):
    c = client()
    batch = create(c, 'L1+L2')
    process_first('L1+L2')
    with db.transaction() as s:
        call = s.scalar(select(db.Call).where(db.Call.run_id == batch['items'][0]['run_id'], db.Call.role == ROLES[2]))
        ev = s.scalar(select(db.Evaluation).join(db.Response).where(db.Response.call_id == call.id))
        evidence = ev.execution_evidence
        evidence['verdict'] = 'violated'
        evidence['checks']['runner_integrity']['verdict'] = 'violated'
        ev.execution_evidence = json.loads(json.dumps(evidence))
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(ev, 'execution_evidence')
    result = analytics(c, batch)
    assert result['event_counts']['server_violation'] == 1
    assert result['event_counts'].get('tool_mismatch', 0) == 0
