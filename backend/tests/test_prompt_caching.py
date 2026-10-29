"""Batch-only prompt caching, using captured requests and an isolated test DB."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import select

from app import batches, db, providers, runtime
from app.config import settings
from app.contracts import ROLES, canonical
from app.prompt_caching import BATCH_PROMPT_CACHE, batch_cached_payload, supports_explicit_caching
from app.worker import claim, process_job
from test_batches import client, create, graphs


def cache_config(**extra):
    return {'batch_id': 'batch', 'batch_prompt_cache': dict(BATCH_PROMPT_CACHE),
            'budget': {'max_input_bytes': 60000}, **extra}


def user_text(payload):
    content = next(m['content'] for m in payload['input'] if m.get('role') == 'user')
    return content if isinstance(content, str) else ''.join(part['text'] for part in content)


def user_parts(payload):
    return next(m['content'] for m in payload['input'] if m.get('role') == 'user')


def wire(data, **extra):
    return {'model': 'gpt-5.6-luna', 'store': False,
            'instructions': 'Frozen policy. <learned_agent_examples>unchanged</learned_agent_examples>',
            'input': [{'role': 'user', 'content': canonical(data)}], **extra}


@pytest.mark.parametrize('kind', ['agent', 'judge'])
def test_static_prefix_matches_across_tasks_and_json_is_lossless(kind):
    data = {'role': 'code_reviewer', 'user_prompt': '검토 "요청"\n\\한 줄',
            'task_input': {'prompt': 'task one'}, 'history': [{'raw_text': 'past one'}],
            'tool_result': None, 'recovery_feedback': {'reason': 'retry one'}}
    if kind == 'judge':
        data = {'role': 'code_reviewer', 'policy': {'rules': ['keep original']},
                'criteria': {'task_fidelity': {'rule': 'SCOPE'}},
                'demonstrations': [{'current_response': 'fixed example'}],
                'reference_material': {'user_prompt': data['user_prompt'], 'task_input': data['task_input']},
                'evaluation_target': {'response_id': 'id-one', 'response_sha256': 'hash-one',
                                      'current_response': 'answer one'},
                'server_checks': {'verdict': 'compliant'}, 'retry_feedback': {'error': 'retry one'}}
    original = wire(data)
    before = deepcopy(original)
    changed = deepcopy(data)
    if kind == 'agent':
        changed.update(task_input={'prompt': 'task two'}, history=[{'raw_text': 'past two'}],
                       tool_result={'status': 'pass'}, recovery_feedback={'reason': 'retry two'})
    else:
        changed['reference_material']['task_input'] = {'prompt': 'task two'}
        changed.update(evaluation_target={'response_id': 'id-two', 'response_sha256': 'hash-two',
                                         'current_response': 'answer two'},
                       server_checks={'verdict': 'violated'}, retry_feedback={'error': 'retry two'})
    first = batch_cached_payload(original, cache_config(), kind)
    second = batch_cached_payload(wire(changed), cache_config(), kind)
    assert original == before
    assert json.loads(user_text(first)) == data
    assert json.loads(user_text(second)) == changed
    assert user_parts(first)[0] == user_parts(second)[0]
    assert user_parts(first)[1] != user_parts(second)[1]
    assert user_parts(first)[0]['prompt_cache_breakpoint'] == {'mode': 'explicit'}
    assert 'prompt_cache_breakpoint' not in user_parts(first)[1]
    assert first['instructions'] == before['instructions']
    assert first['store'] is False
    assert first['prompt_cache_options'] == {'mode': 'explicit', 'ttl': '30m'}
    assert batch_cached_payload(first, cache_config(), kind) is first


def test_recovery_authority_and_tool_continuation_are_preserved():
    original = wire({'role': 'test_runner', 'user_prompt': 'report', 'tool_result': {'status': 'pass'},
                     'recovery_feedback': {'instruction': 'correct this response'}, 'history': []})
    original['input'].insert(0, {'role': 'developer', 'content': 'correct this response'})
    original['input'].extend([
        {'type': 'function_call', 'call_id': 'call', 'name': 'run_original_tests', 'arguments': '{}'},
        {'type': 'function_call_output', 'call_id': 'call', 'output': '{"status":"pass"}'},
    ])
    original.update(tools=[providers.TOOL], parallel_tool_calls=False,
                    text={'format': {'type': 'json_schema', 'name': 'result', 'strict': True, 'schema': {}}})
    new = batch_cached_payload(original, cache_config(), 'agent')
    assert new['input'][0] == original['input'][0]
    assert new['input'][2:] == original['input'][2:]
    assert {k: v for k, v in new.items() if k not in ('input', 'prompt_cache_options')} == {
        k: v for k, v in original.items() if k != 'input'}
    assert json.loads(user_text(new)) == json.loads(user_text(original))


@pytest.mark.parametrize('config', [
    {}, cache_config(batch_id=None), cache_config(batch_prompt_cache=None),
    cache_config(batch_prompt_cache={**BATCH_PROMPT_CACHE, 'version': 'unknown'}),
    cache_config(live_session_id='live'), cache_config(live_stage='submission'),
])
def test_live_standalone_and_frozen_historical_configs_keep_original_payload(config):
    payload = wire({'role': 'code_reviewer', 'user_prompt': 'review', 'task_input': {}})
    assert batch_cached_payload(payload, config, 'agent') is payload


@pytest.mark.parametrize('model,supported', [
    ('gpt-5.6-luna', True), ('gpt-5.6-luna-2026-10-01', True), ('gpt-6-luna', True),
    ('gpt-6.1-sol', True), ('gpt-5.5', False), ('gpt-5.4-nano', False),
    ('gpt-4.1', False), ('o3', False), ('mock', False), ('chat-latest', False), ('gpt-6fake', False),
])
def test_explicit_parameters_only_go_to_supported_models(model, supported):
    assert supports_explicit_caching(model) == supported
    payload = wire({'role': 'code_reviewer', 'task_input': {}}, model=model)
    assert ('prompt_cache_options' in batch_cached_payload(payload, cache_config(), 'agent')) == supported


def test_cache_overhead_does_not_break_input_budget():
    payload = wire({'role': 'code_reviewer', 'user_prompt': 'review', 'task_input': {}})
    limit = len(canonical(payload).encode())
    assert batch_cached_payload(payload, cache_config(budget={'max_input_bytes': limit}), 'agent') is payload


@pytest.mark.parametrize('reference', [{'user_prompt': 'same'}, {'user_prompt': 'same', 'task_input': {}, 'extra': []}])
def test_reference_partition_preserves_empty_and_extra_fields(reference):
    data = {'role': 'code_reviewer', 'reference_material': reference,
            'evaluation_target': {'current_response': '{}'}}
    result = batch_cached_payload(wire(data), cache_config(), 'judge')
    assert json.loads(user_text(result)) == data


@pytest.fixture
def recorded_provider(monkeypatch):
    monkeypatch.setitem(settings.__dict__, 'live_models', ('gpt-5.6-luna',))
    captured = []

    def respond(payload, context):
        captured.append((deepcopy(payload), deepcopy(context)))
        result = providers.mock(payload, context)
        result['usage'] = {'input_tokens': 2000, 'output_tokens': 20, 'total_tokens': 2020,
                           'input_tokens_details': {'cached_tokens': 1200, 'cache_write_tokens': 0}}
        return result

    monkeypatch.setattr(runtime, 'respond', respond)
    return captured


@pytest.mark.parametrize('mode', ['L1', 'FULL'])
def test_batch_worker_persists_exact_cached_payload_and_usage(database, monkeypatch, recorded_provider, mode):
    monkeypatch.setattr(batches, 'dataset_task_ids', lambda: ('HumanEval/0', 'HumanEval/1'))
    batch = create(client(), mode, agent_model='gpt-5.6-luna', judge_model='gpt-5.6-luna',
                   prompts_by_role={r: '[mock:recover]' for r in ROLES})
    assert batch['config_snapshot']['batch_prompt_cache'] == BATCH_PROMPT_CACHE
    agent, offline = graphs()
    while job := claim():
        process_job(job, agent, offline)
    assert recorded_provider
    for payload, context in recorded_provider:
        assert payload['prompt_cache_options'] == {'mode': 'explicit', 'ttl': '30m'}
        assert json.loads(user_text(payload)) == context['data']
    with db.transaction() as s:
        requests = list(s.scalars(select(db.ModelRequest).order_by(db.ModelRequest.created_at)))
        assert [r.request_payload for r in requests] == [p for p, _ in recorded_provider]
        assert all(r.usage['input_tokens_details']['cached_tokens'] == 1200 for r in requests)
        assert any(r.operation_key.endswith(':step1') for r in requests)
        assert any(r.operation_key.startswith('offline:judge:' if mode == 'L1' else 'online:judge:') for r in requests)
        if mode == 'FULL':
            assert any(':agent:v1:' in r.operation_key for r in requests)
        for item in batch['items']:
            assert s.get(db.Run, item['run_id']).config_snapshot['batch_prompt_cache'] == BATCH_PROMPT_CACHE
        for r in requests:
            if ':agent:' in r.operation_key:
                assert r.request_payload['instructions'] == s.get(db.Snapshot, r.input_snapshot_id).role_prompt


def test_old_batch_rerun_gets_new_policy_without_changing_source(database, monkeypatch):
    monkeypatch.setattr(batches, 'dataset_task_ids', lambda: ('HumanEval/0',))
    c = client()
    old = create(c)
    with db.transaction() as s:
        batch = s.get(db.Batch, old['id'])
        batch.config_snapshot = {k: v for k, v in batch.config_snapshot.items() if k != 'batch_prompt_cache'}
        run = s.get(db.Run, old['items'][0]['run_id'])
        run.config_snapshot = {k: v for k, v in run.config_snapshot.items() if k != 'batch_prompt_cache'}
    result = c.post(f"/api/batches/{old['id']}/rerun", json={'mode': 'FULL'},
                    headers={'Idempotency-Key': 'cached-rerun'})
    assert result.status_code == 202, result.text
    assert result.json()['config_snapshot']['batch_prompt_cache'] == BATCH_PROMPT_CACHE
    with db.transaction() as s:
        assert 'batch_prompt_cache' not in s.get(db.Batch, old['id']).config_snapshot
        assert 'batch_prompt_cache' not in s.get(db.Run, old['items'][0]['run_id']).config_snapshot


def test_live_requests_keep_original_layout(database, recorded_provider):
    from test_live import create as create_live, drain, submit
    c = client()
    session = create_live(c, agent_model='gpt-5.6-luna', judge_model='gpt-5.6-luna')
    drain()
    session = c.get('/api/live/sessions/' + session['id']).json()
    assert session['status'] == 'ready'
    assert submit(c, session).status_code == 202
    drain()
    assert recorded_provider
    for payload, context in recorded_provider:
        assert 'prompt_cache_options' not in payload
        assert isinstance(user_parts(payload), str)
        assert json.loads(user_text(payload)) == context['data']
    with db.transaction() as s:
        assert all('batch_prompt_cache' not in r.config_snapshot for r in s.scalars(select(db.Run)))
