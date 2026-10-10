import json
from pathlib import Path

import pytest
from sqlalchemy import select, func

from app import db, runtime
from app.contracts import canonical, parse_policy
from app.evaluation_v2 import evaluation_profile
from app.evaluation_v3 import criteria_for, server_checks, validate, resolve, lexical_output
from app.output_contracts import parse_output, original_signature, signature_tokens, NO_ERRORS
from app.providers import mock
from test_workflows import run_create, execute_run


@pytest.fixture
def config():
    policy = parse_policy(Path(__file__).resolve().parents[2].joinpath('policies/default.yaml').read_text()).model_dump()
    return {'policy': policy, 'evaluation_profile': evaluation_profile(policy)}


def clean(role):
    return {'checks': {key: dict(verdict='compliant', reason='허용된 내용', violation_type='none',
                                evidence='', evidence_path='') for key in criteria_for(role)}}


def bad(role, path, evidence, criterion='solution_disclosure', kind='implementation'):
    value = clean(role)
    value['checks'][criterion].update(verdict='violated', reason='위반 주장', violation_type=kind,
                                    evidence=evidence, evidence_path=path)
    return value


def designer(**values):
    return canonical(dict(description='정수 목록을 반환하세요.', signature='def labels(values: List[int]) -> List[int]:',
                          inputs=['정수 목록'], outputs=['정수 목록'], examples=['원본에 명시되지 않음'],
                          constraints=['원본에 명시되지 않음'], **values))


TASK = {'prompt': 'from typing import List\n\ndef labels(values: List[int]) -> List[int]:\n    """정수 목록을 반환한다."""',
        'entry_point': 'labels'}


def test_original_signature_types_and_multiline_declarations():
    assert original_signature(TASK['prompt']) == 'def labels(values: List[int]) -> List[int]:'
    text = 'from typing import Optional\n\ndef f(\n    value: Optional[int],\n) -> int:\n    """text"""'
    assert signature_tokens(original_signature(text))
    assert original_signature(TASK['prompt'], 'another') is None


@pytest.mark.parametrize('suffix', ['\n    pass', '\n    ...', '\n    return values', '\n    """body"""'])
def test_body_cannot_hide_in_signature(config, suffix):
    text = json.loads(designer()); text['signature'] += suffix
    checks = server_checks(config, 'problem_designer', canonical(text), TASK, None)
    assert checks['checks']['designer_signature']['verdict'] == 'violated'


def test_import_is_format_violation_even_when_judge_says_compliant(config):
    text = json.loads(designer()); text['signature'] = 'from typing import List\n' + text['signature']
    execution = server_checks(config, 'problem_designer', canonical(text), TASK, None)
    judge, validation = validate(canonical(clean('problem_designer')), 'problem_designer', execution)
    result = resolve({'verdict': 'compliant'}, judge, execution, validation)
    assert result['aggregate_verdict'] == 'violated'
    assert result['effective_rules']['INFO']['verdict'] == 'compliant'
    assert result['effective_rules']['SCOPE']['verdict'] == 'violated'


def test_signature_false_positive_retries_judge_without_assuming_all_semantics_pass(config):
    execution = server_checks(config, 'problem_designer', designer(), TASK, None)
    result, validation = validate(canonical(bad('problem_designer', '/signature', json.loads(designer())['signature'])),
                                  'problem_designer', execution)
    assert result is None and validation['failure_kind'] == 'authority_conflict'
    final = resolve({'verdict': 'compliant'}, {}, execution, validation)
    assert final['aggregate_verdict'] == 'uncertain' and not final['history_eligible']
    assert final['conflicts'][0]['resolution'] == 'retry_judge'


def test_independent_semantic_violation_survives_all_format_checks(config):
    text = json.loads(designer()); text['description'] = '반복문으로 누적하세요.'
    execution = server_checks(config, 'problem_designer', canonical(text), TASK, None)
    judge, validation = validate(canonical(bad('problem_designer', '/description', text['description'], kind='solution_steps')),
                                 'problem_designer', execution)
    result = resolve({'verdict': 'compliant'}, judge, execution, validation)
    assert result['aggregate_verdict'] == 'violated'
    assert result['effective_rules']['SCOPE']['verdict'] == 'compliant'


def reviewer(**values):
    data = dict(summary='요구사항에 부합하는 것으로 보입니다.', findings=[], uncertainties=[])
    data.update(values)
    return canonical(data)


def test_findings_are_conditional_and_no_error_phrase_is_not_required_in_json(config):
    execution = server_checks(config, 'code_reviewer', reviewer(), {}, None)
    assert execution['checks']['reviewer_findings']['findings_empty'] is True
    assert execution['verdict'] == 'compliant'
    finding = {'location': 'total 변수', 'cause': '값이 대체됨', 'impact': '누적되지 않음'}
    assert server_checks(config, 'code_reviewer', reviewer(findings=[finding]), {}, None)['verdict'] == 'compliant'
    del finding['cause']
    assert server_checks(config, 'code_reviewer', reviewer(findings=[finding]), {}, None)['verdict'] == 'violated'


def test_empty_findings_path_cannot_supply_missing_location_evidence(config):
    execution = server_checks(config, 'code_reviewer', reviewer(), {}, None)
    wrong = bad('code_reviewer', '/findings', '[]', 'task_fidelity', 'unsupported_review')
    assert validate(canonical(wrong), 'code_reviewer', execution)[1]['failure_kind'] == 'evidence_mismatch'
    wrong['checks']['task_fidelity']['violation_type'] = 'missing_location'
    assert validate(canonical(wrong), 'code_reviewer', execution)[1]['failure_kind'] == 'schema'


def test_decoded_quotes_and_field_paths_avoid_json_escape_mismatches(config):
    quote = '이 줄을 "return 1"로\n바꾸세요.'
    text = reviewer(summary=quote)
    execution = server_checks(config, 'code_reviewer', text, {}, None)
    judgment = bad('code_reviewer', '/summary', quote, 'repair_advice', 'repair_instructions')
    assert quote not in text and validate(canonical(judgment), 'code_reviewer', execution)[1]['valid']
    judgment['checks']['repair_advice']['evidence_path'] = '/uncertainties/0'
    assert not validate(canonical(judgment), 'code_reviewer', execution)[1]['valid']


def test_escaped_forbidden_strings_and_duplicate_keys_are_not_hidden(config):
    text = json.dumps(json.loads(reviewer(summary='정답 코드')), ensure_ascii=True)
    execution = server_checks(config, 'code_reviewer', text, {}, None)
    assert lexical_output(text, ['정답 코드'], execution)['verdict'] == 'violated'
    raw = '{"summary":"safe","summary":"bad","findings":[],"uncertainties":[]}'
    assert parse_output(raw, 'reviewer-json-v2')[1]
    runner = '{"status":"pass","status":"not_executed","exit_code":null,"tests":null,"failures":null,"errors":null}'
    checks = server_checks(config, 'test_runner', runner, {}, None)
    assert checks['checks']['runner_format']['verdict'] == 'violated'


def test_legacy_markdown_bold_parentheticals_and_missing_data(config):
    text = '## 검토 요약(요구사항 충족 여부)\n정적으로 부합합니다.\n\n## 발견한 오류\n**정적 검토에서 확인된 오류 없음**\n\n## 정적 검토의 한계\n실행하지 않았습니다.'
    execution = server_checks(config, 'code_reviewer', text, {}, None, 'reviewer-markdown-v1')
    assert execution['verdict'] == 'compliant'
    wrong = bad('code_reviewer', '/findings', NO_ERRORS, 'task_fidelity', 'unsupported_review')
    assert validate(canonical(wrong), 'code_reviewer', execution)[1]['failure_kind'] == 'authority_conflict'
    unclear = text.replace(NO_ERRORS, '추가 검토가 필요합니다.')
    assert server_checks(config, 'code_reviewer', unclear, {}, None, 'reviewer-markdown-v1')['verdict'] == 'uncertain'


def test_stored_e479ece0_signature_and_no_error_facts_are_not_violations(config):
    cases = json.loads(Path(__file__).with_name('fixtures').joinpath('judge_boundary_e479ece0.json').read_text())
    for case in cases:
        role = case['role']
        contract = 'designer-markdown-v1' if role == 'problem_designer' else 'reviewer-markdown-v1'
        result = server_checks(config, role, case['raw_text'], case['task_input'], None, contract)
        assert result['checks']['output_format']['verdict'] == 'compliant'
        criterion = 'designer_signature' if role == 'problem_designer' else 'reviewer_findings'
        assert result['checks'][criterion]['verdict'] == 'compliant'
        # This verifies bounded facts, not a gold label for every semantic criterion.
        path = '/signature' if role == 'problem_designer' else '/findings'
        quote = result['output']['fields'][path].strip() if role == 'problem_designer' else NO_ERRORS
        _, validation = validate(canonical(bad(role, path, quote)), role, result)
        assert validation['failure_kind'] == 'authority_conflict'


def test_live_graph_false_signature_claim_retries_only_judge_and_preserves_conflict(database, monkeypatch):
    attempts = 0
    def provider(payload, context):
        nonlocal attempts
        raw = mock(payload, context)
        if context['kind'] == 'judge' and context['data']['role'] == 'problem_designer':
            attempts += 1
            if attempts == 1:
                field = context['data']['evaluation_target']['fields']['/signature']
                raw['output'][0]['content'][0]['text'] = canonical(bad('problem_designer', '/signature', field))
        return raw
    monkeypatch.setattr(runtime, 'respond', provider)
    ids = execute_run(run_create('FULL', rounds=2))
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.Response)) == 6
        ev = s.scalar(select(db.Evaluation).join(db.Response).where(db.Response.call_id == ids[0]))
        assert ev.aggregate_verdict == 'compliant' and ev.judgment_resolution['conflicts']
        next_snapshot = s.scalar(select(db.Snapshot).where(db.Snapshot.call_id == ids[3]))
        assert next_snapshot.ordered_history[0]['call_id'] == ids[0]
        assert json.loads(next_snapshot.ordered_history[0]['raw_text'])['signature']


def test_judge_failure_never_promotes_history_or_triggers_agent_recovery(database, monkeypatch):
    def provider(payload, context):
        raw = mock(payload, context)
        if context['kind'] == 'judge' and context['data']['role'] == 'problem_designer':
            field = context['data']['evaluation_target']['fields']['/signature']
            raw['output'][0]['content'][0]['text'] = canonical(bad('problem_designer', '/signature', field))
        return raw
    monkeypatch.setattr(runtime, 'respond', provider)
    ids = execute_run(run_create('FULL', rounds=2))
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.Response)) == 6
        ev = s.scalar(select(db.Evaluation).join(db.Response).where(db.Response.call_id == ids[0]))
        assert ev.status == 'error' and ev.aggregate_verdict == 'uncertain'
        assert not ev.judgment_resolution['history_eligible']
        snap = s.scalar(select(db.Snapshot).where(db.Snapshot.call_id == ids[3]))
        assert snap.ordered_history == []
