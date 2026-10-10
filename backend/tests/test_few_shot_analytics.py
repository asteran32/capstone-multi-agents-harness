"""Count actual cumulative-example applications, not selected pools or API steps."""
from copy import deepcopy

import pytest
from sqlalchemy import func, select

from app import db, runtime
from app.contracts import ROLES, canonical
from app.providers import ProviderError, mock
from app.services import ensure_snapshot
from test_batch_analytics import analytics
from test_batch_exports import client, create, process_first
from test_learned_examples import finish_run, new_run


@pytest.mark.parametrize('mode', ['L1', 'L1+L2', 'FULL'])
def test_applications_separate_initial_recovery_rounds_and_batches(database, mode):
    finish_run(new_run())
    c = client()
    batch = create(c, mode, rounds=2, prompts_by_role={role: '[mock:recover]' for role in ROLES})
    frozen = deepcopy(batch['config_snapshot'])
    assert analytics(c, batch)['few_shot']['applications'] == 0
    process_first(mode)
    # The selected examples come from HumanEval/0: using that same problem
    # must not count, even though L3 recovery and model requests occurred.
    assert analytics(c, batch)['few_shot']['applications'] == 0
    process_first(mode)
    with db.transaction() as s:
        counts_before = [s.scalar(select(func.count()).select_from(model))
                         for model in (db.Snapshot, db.ModelRequest, db.Response, db.Evaluation, db.Log)]
    result = analytics(c, batch)
    expected_recovery = 4 if mode == 'FULL' else 0
    assert result['few_shot'] == {
        'recorded': True, 'enabled': True, 'applications': 4 + expected_recovery,
        'initial': 4, 'recovery': expected_recovery, 'unique_calls': 4, 'unique_examples': 2,
        'roles': [
            {'role': role, 'applications': (2 + expected_recovery // 2) if i < 2 else 0,
             'initial': 2 if i < 2 else 0, 'recovery': expected_recovery // 2 if i < 2 else 0,
             'unique_calls': 2 if i < 2 else 0, 'unique_examples': 1 if i < 2 else 0}
            for i, role in enumerate(ROLES)],
    }
    events = [event for event in result['events'] if event['kind'] == 'few_shot']
    assert len(events) == result['event_counts']['few_shot'] == 4 + expected_recovery
    assert {event['run_id'] for event in events} == {batch['items'][1]['run_id']}
    assert {event['source_task_id'] for event in events} == {'HumanEval/1'}
    assert {event['round_index'] for event in events} == {1, 2}
    assert {event['phase'] for event in events} == {'online'}  # L1's later Judge is not an application.
    assert all(event['examples'][0]['source_task_id'] == 'HumanEval/0' for event in events)
    assert all(event['request_count'] == 1 for event in events)
    assert analytics(c, batch)['few_shot'] == result['few_shot']
    with db.transaction() as s:
        assert counts_before == [s.scalar(select(func.count()).select_from(model))
                                 for model in (db.Snapshot, db.ModelRequest, db.Response, db.Evaluation, db.Log)]
        assert s.get(db.Batch, batch['id']).config_snapshot == frozen
    assert analytics(c, create(c, mode))['few_shot']['applications'] == 0


def test_multiple_examples_are_one_application_per_response(database):
    finish_run(new_run())
    finish_run(new_run(1))
    c = client()
    batch = create(c, 'FULL')
    finish_run(batch['items'][2]['run_id'])
    result = analytics(c, batch)
    assert result['few_shot']['applications'] == result['few_shot']['initial'] == 2
    assert result['few_shot']['unique_examples'] == 4
    assert result['few_shot']['recovery'] == 0
    assert [len(event['example_ids']) for event in result['events'] if event['kind'] == 'few_shot'] == [2, 2]


def test_transport_retry_and_runner_tool_followup_do_not_inflate_count(database, monkeypatch):
    def train_runner(payload, context):
        result = mock(payload, context)
        if (context['kind'] == 'agent' and context['role'] == 'test_runner' and context['version'] == 0
                and result['output'][0]['type'] == 'message'):
            result['output'][0]['content'][0]['text'] = canonical(
                {'status': 'pass', 'exit_code': 0, 'tests': 1, 'failures': 0, 'errors': 0})
        return result
    monkeypatch.setattr(runtime, 'respond', train_runner)
    finish_run(new_run())
    retried = False
    def retry_reviewer(payload, context):
        nonlocal retried
        if context['kind'] == 'agent' and context['role'] == 'code_reviewer' and not retried:
            retried = True
            raise ProviderError('synthetic transient failure', retryable=True)
        return mock(payload, context)
    monkeypatch.setattr(runtime, 'respond', retry_reviewer)
    c = client()
    batch = create(c, 'FULL')
    finish_run(batch['items'][1]['run_id'])
    result = analytics(c, batch)
    assert result['few_shot']['applications'] == result['few_shot']['initial'] == 3
    assert result['few_shot']['unique_examples'] == 3
    events = {event['role']: event for event in result['events'] if event['kind'] == 'few_shot'}
    assert events['code_reviewer']['request_count'] == 2
    assert events['code_reviewer']['request_states'] == {'failed': 1, 'completed': 1}
    assert events['test_runner']['request_count'] == 2
    assert events['test_runner']['request_states'] == {'completed': 2}


@pytest.mark.parametrize('state', ['failed', 'running', 'unknown'])
def test_attempt_requires_agent_request_and_actual_prompt_but_not_completed_response(database, state):
    finish_run(new_run())
    c = client()
    batch = create(c, 'FULL')
    with db.transaction() as s:
        call = s.scalar(select(db.Call).where(db.Call.run_id == batch['items'][1]['run_id'],
                                             db.Call.role == 'code_reviewer'))
        snapshot = ensure_snapshot(s, call, 0)
        snapshot_id, call_id = snapshot.id, call.id
        prompt = snapshot.role_prompt
        assert '<learned_agent_examples>' in prompt
        # Neither a saved input nor online/offline Judge requests count.
        for operation in ('online:judge:v0:attempt1', 'offline:judge:v0:attempt1'):
            s.add(db.ModelRequest(call_id=call.id, input_snapshot_id=snapshot.id, operation_key=operation,
                                  attempt_number=1, request_payload={}, status=state))
    assert analytics(c, batch)['few_shot']['applications'] == 0
    with db.transaction() as s:
        s.add(db.ModelRequest(call_id=call_id, input_snapshot_id=snapshot_id, operation_key='online:agent:v0:step0',
                              attempt_number=1, request_payload={'instructions': prompt}, status=state))
    result = analytics(c, batch)
    assert result['few_shot']['applications'] == result['few_shot']['initial'] == 1
    event = next(event for event in result['events'] if event['kind'] == 'few_shot')
    assert event['request_states'] == {state: 1}
    with db.transaction() as s:
        # A config selection or stray example ID does not prove prompt inclusion.
        s.get(db.Snapshot, snapshot_id).role_prompt = 'Only the example ID: ' + event['example_ids'][0]
    assert analytics(c, batch)['few_shot']['applications'] == 0


@pytest.mark.parametrize('setting', ['empty', 'off', 'legacy'])
def test_disabled_empty_and_legacy_records_are_distinguished(database, setting):
    c = client()
    if setting != 'empty':
        finish_run(new_run())
    batch = create(c, 'FULL', learned_few_shot=setting != 'off')
    if setting == 'legacy':
        with db.transaction() as s:
            run_ids = [item['run_id'] for item in batch['items']]
            records = [s.get(db.Batch, batch['id']), *s.scalars(select(db.Run).where(db.Run.id.in_(run_ids)))]
            for record in records:
                record.config_snapshot = {key: value for key, value in record.config_snapshot.items()
                                          if key not in ('learned_few_shot', 'few_shot_snapshot')}
    finish_run(batch['items'][1]['run_id'])
    result = analytics(c, batch)
    assert result['few_shot']['applications'] == 0
    assert result['few_shot']['recorded'] == (setting != 'legacy')
    assert result['few_shot']['enabled'] == (setting == 'empty')
    assert not any(event['kind'] == 'few_shot' for event in result['events'])
