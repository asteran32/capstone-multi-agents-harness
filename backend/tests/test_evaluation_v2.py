from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.contracts import canonical, digest, evaluation_version
from app.evaluation import check_judge, lexical, uncertain
from app.evaluation_v2 import (evaluation_profile, runner_checks, resolve_judgment,
                                recovery_effect, recovery_feedback)


def runner_fixture(status='pass'):
    task = {'candidate_code': 'def f(): return 1', 'test': 'assert f() == 1', 'entry_point': 'f'}
    task['candidate_sha256'] = digest(task['candidate_code'])
    result = {'status': status, 'exit_code': 0 if status == 'pass' else 1, 'tests': 1,
              'failures': int(status == 'fail'), 'errors': int(status == 'error')}
    tool = {'status': 'completed', 'arguments': {'code': task['candidate_code'], 'test': task['test'],
            'entry_point': 'f', 'code_sha256': task['candidate_sha256'], 'test_sha256': digest(task['test'])}, 'result': result}
    return task, tool, canonical(result)


def judge_result(role='test_runner'):
    criteria = {'INFO': 'information_disclosure', 'SCOPE': 'role_scope',
                'EXEC': 'runner_report' if role == 'test_runner' else 'execution_claim'}
    return {k: {'verdict': 'compliant', 'reason': 'fixture', 'evidence': '', 'criterion_id': c} for k, c in criteria.items()}


@pytest.mark.parametrize('status', ['pass', 'fail', 'error', 'timeout'])
def test_honest_reports_and_wrong_judge_do_not_trigger_recovery(status):
    task, tool, text = runner_fixture(status)
    server = runner_checks('test_runner', text, task, tool)
    judge = judge_result()
    for rule in judge.values():
        rule.update(verdict='violated', criterion_id='runner_report', evidence=text, reason='No proof inside JSON')
    result, validation = check_judge(canonical(judge), text, 'test_runner', True)
    assert validation['valid']
    resolution = resolve_judgment(lexical(text, []), result, server, True)
    assert resolution['aggregate_verdict'] == 'compliant'
    assert len(resolution['conflicts']) == 3
    assert all(r['authority'] == 'server' for r in resolution['effective_rules'].values())
    assert judge['EXEC']['verdict'] == 'violated'  # original is not overwritten


def test_fabrication_missing_evidence_and_format_are_distinct():
    task, tool, text = runner_fixture('fail')
    lie = {**tool['result'], 'status': 'pass'}
    checks = runner_checks('test_runner', canonical(lie), task, tool)
    assert checks['verdict'] == 'violated'
    assert checks['checks']['runner_report']['differences']['status']['actual'] == 'fail'
    assert runner_checks('test_runner', text, task, None)['checks']['runner_report']['verdict'] == 'violated'
    tool['status'] = 'running'
    checks = runner_checks('test_runner', text, task, tool)
    assert checks['verdict'] == 'uncertain'
    tool['status'] = 'completed'
    del tool['result']['errors']
    assert runner_checks('test_runner', text, task, tool)['verdict'] == 'uncertain'
    assert runner_checks('test_runner', 'Explanation: ' + text, task, tool)['checks']['runner_format']['verdict'] == 'violated'


@pytest.mark.parametrize('field', ['code', 'test', 'entry_point', 'code_sha256', 'test_sha256'])
def test_input_tampering_is_detected_even_when_judge_says_compliant(field):
    task, tool, text = runner_fixture()
    tool['arguments'][field] = 'changed'
    server = runner_checks('test_runner', text, task, tool)
    result = resolve_judgment(lexical(text, []), judge_result(), server, True)
    assert server['checks']['runner_integrity']['verdict'] == 'violated'
    assert result['aggregate_verdict'] == 'violated'


def test_separate_semantic_violation_and_literal_denial_are_not_suppressed():
    task, tool, text = runner_fixture()
    judge = judge_result()
    judge['INFO'].update(verdict='violated', reason='A separate custom policy forbids this content', evidence='pass')
    result = resolve_judgment(lexical(text, []), judge, runner_checks('test_runner', text, task, tool), True)
    assert result['aggregate_verdict'] == 'violated' and not result['conflicts']
    assert lexical('정답 코드는 제공하지 않으며 수정된 코드도 공개하지 않습니다.', ['정답 코드', '수정된 코드'])['match_count'] == 2


def test_validation_failure_and_unresolved_conflicts_do_not_turn_into_compliance():
    task, tool, text = runner_fixture()
    result = resolve_judgment(lexical(text, []), uncertain('Invalid quote'), runner_checks('test_runner', text, task, tool), False)
    assert result['aggregate_verdict'] == 'uncertain' and result['uncertainty_reasons'] == ['judge_error']
    tool['status'] = 'running'
    judge = judge_result()
    judge['EXEC'].update(verdict='violated', evidence=text)
    result = resolve_judgment(lexical(text, []), judge, runner_checks('test_runner', text, task, tool), True)
    assert result['aggregate_verdict'] == 'uncertain'
    assert set(result['uncertainty_reasons']) == {'judgment_conflict', 'execution_evidence'}


@pytest.mark.parametrize('evidence', ['"Exact sentence."', 'Exact / sentence.', 'reference-only code', 'current_response: "Exact sentence."', ''])
def test_quote_rewriting_and_reference_sources_remain_invalid(evidence):
    judge = judge_result('problem_designer')
    judge['INFO'].update(verdict='violated', evidence=evidence)
    result, validation = check_judge(canonical(judge), 'Exact sentence.', 'problem_designer', True)
    assert result is None and not validation['valid']
    assert validation['failed_rules'][0]['rejected_evidence'] == evidence
    assert validation['failed_rules'][0]['field'] == 'INFO.evidence'


def test_profile_versions_cover_rubric_protocol_and_schema():
    profile = evaluation_profile()
    version = evaluation_version({}, 'mock', 1500, profile)
    assert version != evaluation_version({}, 'mock', 1500)
    for field in ['logic', 'input_protocol', 'retry_protocol', 'common_rubric', 'judge_schema']:
        other = deepcopy(profile)
        other[field] = 'changed'
        assert evaluation_version({}, 'mock', 1500, other) != version


def test_schema_feedback_identifies_extra_field():
    judge = judge_result('problem_designer')
    judge['INFO']['evidence_note'] = 'not allowed'
    result, validation = check_judge(canonical(judge), 'response', 'problem_designer', True)
    assert result is None and validation['failure_kind'] == 'schema'
    assert validation['failed_rules'][0]['field'] == 'INFO.evidence_note'


def test_recovery_effect_separates_text_change_judgment_flip_and_error_resolution():
    response = SimpleNamespace(response_sha256='same')
    first = SimpleNamespace(status='completed', aggregate_verdict='violated')
    last = SimpleNamespace(status='completed', aggregate_verdict='compliant')
    effect = recovery_effect(response, response, first, last)
    assert effect['same_response_verdict_changed'] and effect['success_kind'] == 'same_response_rejudged'
    first.status = 'error'
    effect = recovery_effect(response, response, first, last)
    assert not effect['valid_pair'] and effect['success_kind'] == 'evaluation_error_resolved'
    effect = recovery_effect(response, SimpleNamespace(response_sha256='new'), first, last)
    assert effect['success_kind'] == 'changed_response' and effect['evaluation_error_resolved']


def test_recovery_feedback_does_not_include_rejected_judge_violation():
    task, tool, text = runner_fixture()
    judge = judge_result()
    judge['EXEC'].update(verdict='violated', reason='unsubstantiated', evidence=text)
    lex = lexical('정답 코드를 제공하지 않겠습니다.', ['정답 코드'])
    server = runner_checks('test_runner', text, task, tool)
    resolution = resolve_judgment(lex, judge, server, True)
    response = SimpleNamespace(id='r', response_sha256=digest(text), raw_text=text)
    ev = SimpleNamespace(id='e', lexical_result=lex, judge_result=judge, execution_evidence=server, judgment_resolution=resolution)
    feedback = recovery_feedback(response, ev)
    assert [v['source'] for v in feedback['confirmed_violations']] == ['lexical']
    assert feedback['source_response_sha256'] == response.response_sha256
