"""Check demonstration contracts and request wiring without calling a provider."""
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from app.contracts import (JudgePromptDocument, ROLES, RunnerReport, canonical, digest,
                           evaluation_version, parse_policy, render_policy)
from app.evaluation import check_judge, lexical
from app.evaluation_v2 import evaluation_profile, judge_payload
from app.evaluation_v3 import resolve, server_checks, validate


def policy():
    return parse_policy((Path(__file__).parents[2] / 'policies/default.yaml').read_text()).model_dump()


def test_agent_demonstrations_do_not_teach_forbidden_literals_or_fabricated_reports():
    document = policy()
    for role in document['roles']:
        assert role['few_shot_examples']
        for example in role['few_shot_examples']:
            assert lexical(example['output'], document['forbidden_terms'])['verdict'] == 'compliant'
            if role['role'] == 'test_runner':
                parsed = RunnerReport.model_validate_json(example['output']).model_dump()
                assert parsed == example['input']['tool_result']


def test_reviewer_v3_examples_survive_evidence_validation_and_control_history_eligibility():
    """Validate authored labels and aggregation, not live model classification accuracy."""
    document = policy()
    profile = evaluation_profile(document)
    config = {'policy': document, 'evaluation_profile': profile}
    for example in profile['few_shot_examples']['code_reviewer']:
        text = example['current_response']
        execution = server_checks(config, 'code_reviewer', text,
                                  example['reference_material']['task_input'], None)
        # These examples isolate semantic breaches from format/missing-field problems.
        assert execution['verdict'] == 'compliant', example['id']
        judgment, validation = validate(canonical(example['expected']), 'code_reviewer', execution)
        assert validation['valid'], (example['id'], validation)
        result = resolve(lexical(text, document['forbidden_terms']), judgment, execution, validation)
        violations = [c for c in example['expected']['checks'].values() if c['verdict'] == 'violated']
        assert result['aggregate_verdict'] == ('violated' if violations else 'compliant'), example['id']
        assert result['history_eligible'] is (not violations), example['id']


def test_reviewer_agent_and_judge_share_the_same_positive_examples():
    document = policy()
    role = next(r for r in document['roles'] if r['role'] == 'code_reviewer')
    judge_examples = {e['id']: e for e in evaluation_profile(document)['few_shot_examples']['code_reviewer']}
    for example in role['few_shot_examples']:
        paired = judge_examples[example['id']]
        assert paired['reference_material']['task_input'] == example['input']['task_input']
        assert paired['current_response'] == example['output']
        assert all(check['verdict'] == 'compliant' for check in paired['expected']['checks'].values())


@pytest.mark.parametrize('role', ROLES)
def test_judge_demonstrations_pass_the_same_schema_and_quote_checks_as_real_outputs(role):
    examples = evaluation_profile()['few_shot_examples'][role]
    for example in examples:
        result, validation = check_judge(canonical(example['expected']), example['current_response'], role, True)
        assert validation['valid'], (example['id'], validation)
        if role == 'test_runner':
            report = example['server_checks'].get('checks', {}).get('runner_report')
            assert result['EXEC']['verdict'] == (report['verdict'] if report else 'uncertain')


def test_examples_are_scoped_to_the_role_and_never_replace_the_actual_target():
    profile = evaluation_profile()
    config = {'evaluation_profile': profile, 'policy': policy(), 'judge_model': 'mock',
              'budget': {'max_output_tokens': 1500}}
    target = 'Unique actual response; not a demonstration.'
    for role in ROLES:
        payload, data = judge_payload(config, role, 'actual-id', digest(target), target,
                                      {'prompt': 'Actual problem'}, 'Actual request', {})
        assert data['evaluation_target']['current_response'] == target
        assert target not in payload['instructions']
        assert 'few_shot_examples' not in data['policy']
        for other in ROLES:
            for example in profile['few_shot_examples'][other]:
                assert (example['id'] in payload['instructions']) == (other == role)


def test_old_policy_and_frozen_judge_profiles_without_examples_remain_usable():
    document = policy()
    for role in document['roles']:
        del role['few_shot_examples']
        assert '<agent_examples>' not in render_policy(role, True)
    parsed = parse_policy(yaml.safe_dump(document))
    assert all(not r.few_shot_examples for r in parsed.roles)
    assert parsed.model_dump() == document  # Frozen evaluator policy equality/hash must survive upgrades.
    profile = evaluation_profile()
    del profile['few_shot_examples']
    config = {'evaluation_profile': profile, 'policy': document, 'judge_model': 'mock',
              'budget': {'max_output_tokens': 1500}}
    for role in ROLES:
        payload, _ = judge_payload(config, role, 'r', 'sha', 'response', {}, 'request', {})
        assert payload['instructions'] == profile['common_rubric'] + '\n' + profile['role_rubrics'][role]


def test_editing_only_an_example_changes_the_evaluation_version():
    document, profile = policy(), evaluation_profile()
    original = evaluation_version(document, 'mock', 1500, profile)
    updated = deepcopy(profile)
    updated['few_shot_examples']['code_reviewer'][0]['expected']['INFO']['reason'] = 'Revised label explanation'
    assert evaluation_version(document, 'mock', 1500, updated) != original
    document['roles'][0]['few_shot_examples'][0]['boundary_note'] = 'Revised agent boundary explanation'
    assert evaluation_version(document, 'mock', 1500, profile) != original


@pytest.mark.parametrize('problem', ['wrong_role_criterion', 'quote_from_reference', 'nonempty_compliant_evidence', 'duplicate_id'])
def test_invalid_judge_examples_are_rejected_before_any_requests(problem):
    profile = evaluation_profile()
    document = {k: profile[k] for k in ['common_rubric', 'role_rubrics', 'retry_instruction', 'few_shot_examples']}
    examples = document['few_shot_examples']['code_reviewer']
    example = examples[0]
    if problem == 'wrong_role_criterion':
        example['expected']['EXEC']['criterion_id'] = 'runner_report'
    elif problem == 'quote_from_reference':
        example['expected']['INFO'].update(verdict='violated', evidence='return value >= 0')
    elif problem == 'nonempty_compliant_evidence':
        example['expected']['INFO']['evidence'] = 'not empty'
    else:
        examples.append(deepcopy(example))
    with pytest.raises(ValueError):
        JudgePromptDocument.model_validate(document)
