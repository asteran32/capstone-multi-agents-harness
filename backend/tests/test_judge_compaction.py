import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from app import evaluation_v2
from app.contracts import canonical, digest, evaluation_version, parse_policy
from app.evaluation_v2 import evaluation_profile, judge_payload
from app.evaluation_v3 import compact_examples, server_checks, validate
from app.output_contracts import MODELS, contract_for, parse_output
from app.providers import mock


@pytest.fixture
def config():
    policy = parse_policy(Path(__file__).resolve().parents[2].joinpath('policies/default.yaml').read_text()).model_dump()
    return dict(policy=policy, evaluation_profile=evaluation_profile(policy), judge_model='mock',
                budget={'max_output_tokens': 1500})


def expand_examples(bundle):
    examples = []
    for case in bundle['cases']:
        value = {k: v for k, v in case.items() if k not in ('reference_id', 'expected_check_ids')}
        if 'reference_id' in case:
            value['reference_material'] = bundle['references'][case['reference_id']]
        value['expected'] = {'checks': {key: bundle['judgments'][index]
                                       for key, index in case['expected_check_ids'].items()}}
        examples.append(value)
    return examples


def test_split_judge_files_preserve_definitions_and_fail_on_invalid_legacy_file(tmp_path, monkeypatch):
    document = evaluation_v2.load_judge_prompts()
    current = tmp_path / 'judge.yaml'
    legacy = tmp_path / 'judge_legacy.yaml'
    current.write_text(yaml.safe_dump({'structured_evaluation': document['structured_evaluation']}))
    legacy.write_text(yaml.safe_dump({k: v for k, v in document.items() if k != 'structured_evaluation'}))
    monkeypatch.setattr(evaluation_v2, 'JUDGE_POLICY', current)
    assert evaluation_v2.load_judge_prompts() == document
    for invalid in ('[]', 'null', 'invalid scalar'):
        legacy.write_text(invalid)
        with pytest.raises(ValueError, match='Judge 프롬프트 파일'):
            evaluation_v2.load_judge_prompts()
    legacy.unlink()
    with pytest.raises(ValueError, match='Judge 프롬프트 파일'):
        evaluation_v2.load_judge_prompts()


def test_reviewer_three_field_contract_and_historical_four_field_contract(config):
    report = dict(summary='정적으로 검토했습니다.', findings=[], uncertainties=[])
    current = canonical(report)
    legacy = canonical({**report, 'limitations': '정적 검토의 한계입니다.'})
    assert parse_output(current, 'reviewer-json-v2') == (report, None)
    assert parse_output(legacy, 'reviewer-json-v2')[1]  # Removed fields are not silently accepted.
    assert parse_output(legacy, 'reviewer-json-v1')[1] is None
    assert parse_output(current, 'reviewer-json-v1')[1]
    reviewer = next(p for p in config['policy']['roles'] if p['role'] == 'code_reviewer')
    assert contract_for(reviewer) == 'reviewer-json-v2'
    old = {**reviewer, 'output_contract': 'reviewer-json-v1'}
    assert contract_for(old) == 'reviewer-json-v1'
    assert MODELS['reviewer-json-v2'].model_json_schema()['required'] == ['summary', 'findings', 'uncertainties']
    assert 'limitations' not in canonical(reviewer)
    for contract in ('reviewer-json-v1', 'reviewer-json-v2'):
        output = mock({}, {'kind': 'agent', 'role': 'code_reviewer', 'version': 0,
                          'output_contract': contract, 'data': {'user_prompt': 'review'}})
        raw = output['output'][0]['content'][0]['text']
        assert parse_output(raw, contract)[1] is None
        assert ('limitations' in json.loads(raw)) is (contract == 'reviewer-json-v1')


@pytest.mark.parametrize('role', ['problem_designer', 'code_reviewer', 'test_runner'])
def test_shared_examples_preserve_every_case_fact_label_and_evidence(config, role):
    examples = config['evaluation_profile']['few_shot_examples'][role]
    before = canonical(examples)
    bundle = compact_examples(examples)
    assert expand_examples(bundle) == examples
    assert canonical(examples) == before
    if role == 'code_reviewer':
        assert len(bundle['cases']) == 10
        assert len(canonical(bundle)) < len(before) * .7
    for example in expand_examples(bundle):
        execution = server_checks(config, role, example['current_response'],
                                  example.get('reference_material', {}).get('task_input', {}), None)
        assert validate(canonical(example['expected']), role, execution)[1]['valid']


@pytest.mark.parametrize('role', ['problem_designer', 'code_reviewer', 'test_runner'])
def test_compact_request_preserves_target_reference_and_trusted_checks(config, role):
    example = config['evaluation_profile']['few_shot_examples'][role][0]
    text = example['current_response']
    task = example.get('reference_material', {}).get('task_input', {})
    execution = server_checks(config, role, text, task, None)
    before = canonical(execution)
    feedback = dict(failure_kind='evidence_mismatch', failed_rules=['task_fidelity'], error='Exact quote required',
                    judge_output='Previous fallible verdict')
    payload, data = judge_payload(config, role, 'id', digest(text), text, task, 'request', execution, feedback)
    assert data['evaluation_target']['current_response'] == text
    assert data['evaluation_target']['fields'] == execution['output']['fields']
    assert data['reference_material'] == {'task_input': task, 'user_prompt': 'request'}
    assert data['server_checks']['checks'] == execution['checks']
    assert data['server_checks']['output'] == {k: execution['output'][k] for k in ('contract', 'protected_evidence')}
    assert 'parsed' not in data['server_checks']['output'] and 'fields' not in data['server_checks']['output']
    assert all(data['retry_feedback'][key] == value for key, value in feedback.items())
    assert canonical(execution) == before
    assert json.loads(payload['input'][-1]['content']) == data
    bundle = json.loads(payload['instructions'].split('<judge_examples>\n')[1].split('\n</judge_examples>')[0])
    assert expand_examples(bundle) == config['evaluation_profile']['few_shot_examples'][role]


def test_frozen_v3_protocol_keeps_full_examples_and_server_facts(config):
    old = deepcopy(config)
    profile = old['evaluation_profile']
    profile.update(logic='guardrail-3.0', input_protocol='field-evidence-1', common_rubric='Frozen old rubric')
    profile['output_contracts']['code_reviewer'] = 'reviewer-json-v1'
    text = canonical(dict(summary='Old review', findings=[], uncertainties=[], limitations='No execution'))
    execution = server_checks(old, 'code_reviewer', text, {}, None)
    payload, data = judge_payload(old, 'code_reviewer', 'id', digest(text), text, {}, 'request', execution)
    assert execution['checks']['output_format']['verdict'] == 'compliant'
    assert data['server_checks'] == execution
    examples = json.loads(payload['instructions'].split('<judge_examples>\n')[1].split('\n</judge_examples>')[0])
    assert examples == profile['few_shot_examples']['code_reviewer']
    assert evaluation_version(old['policy'], 'mock', 1500, profile) != evaluation_version(
        config['policy'], 'mock', 1500, config['evaluation_profile'])
