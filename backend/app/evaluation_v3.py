"""Criterion-scoped evaluation with immutable contracts and field-local evidence."""
from copy import deepcopy

from .contracts import canonical
from .evaluation import lexical
from .output_contracts import MODELS, ROLE_CONTRACTS, contract_for, output_checks, unique_json

CRITERIA = {
    'solution_disclosure': {'rule': 'INFO', 'authority': 'judge',
        'types': ['implementation', 'pseudocode', 'solution_steps', 'candidate_code_quote']},
    'repair_advice': {'rule': 'INFO', 'authority': 'judge', 'types': ['replacement_code', 'repair_instructions']},
    'task_fidelity': {'rule': 'SCOPE', 'authority': 'judge',
        'types': ['changed_requirement', 'invented_requirement', 'invented_example', 'invented_constraint',
                  'unsupported_review', 'internal_contradiction', 'unqualified_correctness', 'unauthorized_action']},
    'execution_claim': {'rule': 'EXEC', 'authority': 'judge', 'types': ['unsupported_execution_claim']},
}


def criteria_for(role):
    return {key: value for key, value in CRITERIA.items() if role == 'code_reviewer' or key != 'repair_advice'}


def judge_schema(role):
    checks = {}
    for criterion, spec in criteria_for(role).items():
        fields = {'verdict': {'type': 'string', 'enum': ['compliant', 'violated', 'uncertain']},
                  'reason': {'type': 'string', 'minLength': 1, 'pattern': r'\S'},
                  'evidence': {'type': 'string'}, 'evidence_path': {'type': 'string'},
                  'violation_type': {'type': 'string', 'enum': ['none', 'unclear', *spec['types']]}}
        checks[criterion] = {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}
    return {'type': 'object', 'properties': {'checks': {'type': 'object', 'properties': checks,
            'required': list(checks), 'additionalProperties': False}}, 'required': ['checks'], 'additionalProperties': False}


def profile(policy, prompts):
    instructions = prompts.get('structured_evaluation')
    if not instructions or set(instructions) != {'common_rubric', 'role_rubrics', 'retry_instruction', 'few_shot_examples'}:
        raise ValueError('Judge v3 지침과 예시가 필요합니다.')
    if set(instructions['role_rubrics']) != {'problem_designer', 'code_reviewer', 'test_runner'}:
        raise ValueError('Judge v3에 모든 역할 지침이 필요합니다.')
    contracts = {p['role']: contract_for(p) for p in policy['roles']}
    for role, examples in instructions['few_shot_examples'].items():
        ids = set()
        for example in examples:
            if not example.get('id') or example['id'] in ids:
                raise ValueError('Judge v3 예시 ID는 역할 내에서 고유해야 합니다.')
            ids.add(example['id'])
            output = output_checks(role, example['current_response'], example.get('reference_material', {}).get('task_input', {}),
                                   ROLE_CONTRACTS[role])
            _, checked = validate(canonical(example['expected']), role, {'output': output})
            if not checked['valid']:
                raise ValueError(f"Judge v3 예시가 출력·인용 계약에 맞지 않습니다: {role}/{example['id']}")
    return {'version': 3, 'logic': 'guardrail-3.2', 'input_protocol': 'field-evidence-1',
            'retry_protocol': 'validation-feedback-2', **deepcopy(instructions),
            'criteria': deepcopy(CRITERIA), 'output_contracts': contracts,
            'output_schemas': {k: v.model_json_schema() for k, v in MODELS.items()},
            'judge_schemas': {r: judge_schema(r) for r in contracts},
            'server_checks_version': 'role-format-1', 'history_protocol': 'effective-compliance-1'}


def server_checks(config, role, text, task_input, tool, contract=None):
    from .evaluation_v2 import runner_checks, combine
    result = runner_checks(role, text, task_input, tool)
    output = output_checks(role, text, task_input, contract or config['evaluation_profile']['output_contracts'][role])
    if role != 'test_runner':
        result['checks'] = output['checks']
    else:
        if output['format_error']:
            result['checks']['runner_format'].update(verdict='violated', reason=output['format_error'])
        for key, value in result['checks'].items():
            value.update(rule='EXEC' if key in ('runner_report', 'runner_execution') else 'SCOPE', authority='server')
        if not output['format_error'] and result['checks']['runner_report']['verdict'] == 'compliant':
            output['protected_evidence'].extend({'path': path, 'text': value, 'kind': 'verified_runner_report'}
                                                for path, value in output['fields'].items())
    return {**result, 'schema_version': 3, 'applicable': True,
            'verdict': combine([v['verdict'] for v in result['checks'].values()]),
            'output': output}


def lexical_output(text, terms, execution):
    result = lexical(text, terms)
    # Inspect decoded values too: JSON unicode escapes must not hide a forbidden string.
    for path, value in execution.get('output', {}).get('fields', {}).items():
        for match in lexical(value, terms)['matches']:
            if not any(m['normalized_term'] == match['normalized_term'] for m in result['matches']):
                result['matches'].append({**match, 'field_path': path, 'representation': 'decoded'})
    result.update(verdict='violated' if result['matches'] else 'compliant', match_count=len(result['matches']),
                  distinct_term_count=len({m['normalized_term'] for m in result['matches']}))
    return result


def judge_payload(config, role, response_id, response_sha256, text, task_input, user_prompt, execution, retry_feedback=None):
    p = config['evaluation_profile']
    policy = next(v for v in config['policy']['roles'] if v['role'] == role)
    data = {'role': role, 'policy': {k: v for k, v in policy.items() if k != 'few_shot_examples'},
            'evaluation_target': {'response_id': response_id, 'response_sha256': response_sha256,
                                  'current_response': text, 'fields': execution['output']['fields']},
            'reference_material': {'task_input': task_input, 'user_prompt': user_prompt},
            'server_checks': execution, 'criteria': {k: p['criteria'][k] for k in criteria_for(role)}}
    if retry_feedback:
        data['retry_feedback'] = {k: retry_feedback.get(k) for k in ('failure_kind', 'failed_rules', 'error', 'judge_output')}
        data['retry_feedback']['instruction'] = p['retry_instruction']
    instructions = p['common_rubric'] + '\n' + p['role_rubrics'][role]
    examples = p.get('few_shot_examples', {}).get(role, [])
    if examples:
        instructions += '\n<judge_examples>\n' + canonical(examples) + '\n</judge_examples>'
    return {'model': config['judge_model'], 'store': False, 'instructions': instructions,
            'input': [{'role': 'user', 'content': canonical(data)}],
            'max_output_tokens': config['budget']['max_output_tokens'],
            'text': {'format': {'type': 'json_schema', 'name': 'role_judgment', 'strict': True,
                                'schema': p['judge_schemas'][role]}}}, data


def summarize(checks):
    from .evaluation_v2 import combine
    result = {}
    for rule in ('INFO', 'SCOPE', 'EXEC'):
        selected = [dict(criterion_id=key, **value) for key, value in checks.items() if value['rule'] == rule]
        verdict = combine([c['verdict'] for c in selected])
        decisive = [c for c in selected if c['verdict'] == verdict]
        result[rule] = {'verdict': verdict, 'reason': '\n'.join(c['reason'] for c in decisive) or '해당 위반 없음',
                        'evidence': next((c.get('evidence', '') for c in decisive if c.get('evidence')), ''),
                        'authority': 'mixed' if len({c['authority'] for c in selected}) > 1 else
                                     next((c['authority'] for c in selected), 'server'), 'criteria': selected}
    return result


def validate(raw, role, execution):
    specs = criteria_for(role)
    validation = {'schema_version': 4, 'valid': False, 'schema_valid': False, 'evidence_valid': None,
                  'evidence_required': None, 'failure_kind': None, 'failed_rules': [], 'error': None,
                  'authority_conflicts': []}
    try:
        data = unique_json(raw)
        if not isinstance(data, dict) or set(data) != {'checks'} or not isinstance(data['checks'], dict) or set(data['checks']) != set(specs):
            raise ValueError('Required semantic criteria must appear exactly once; server criteria are not Judge criteria')
        for key, value in data['checks'].items():
            if not isinstance(value, dict) or set(value) != {'verdict', 'reason', 'evidence', 'evidence_path', 'violation_type'}:
                raise ValueError(f'{key}: incorrect fields')
            if any(not isinstance(v, str) for v in value.values()) or not value['reason'].strip():
                raise ValueError(f'{key}: fields must be strings and reason must be nonempty')
            expected_types = specs[key]['types'] if value['verdict'] == 'violated' else ['none'] if value['verdict'] == 'compliant' else ['unclear']
            if value['verdict'] not in ('compliant', 'violated', 'uncertain') or value['violation_type'] not in expected_types:
                raise ValueError(f'{key}: invalid verdict / violation_type')
        validation['schema_valid'] = True
    except (ValueError, TypeError) as exc:
        return None, {**validation, 'failure_kind': 'schema', 'error': str(exc)}
    fields = execution['output']['fields']
    failures, checked = [], {}
    required = any(v['verdict'] == 'violated' for v in data['checks'].values())
    for key, value in data['checks'].items():
        quote, path = value['evidence'], value['evidence_path']
        kind = None
        if value['verdict'] == 'violated':
            if not quote.strip():
                kind = 'evidence_missing'
            elif path not in fields or quote not in fields[path]:
                kind = 'evidence_mismatch'
            else:
                for allowed in execution['output']['protected_evidence']:
                    if path in (allowed['path'], '$') and quote in allowed['text']:
                        kind = 'authority_conflict'
                        validation['authority_conflicts'].append({'rule': specs[key]['rule'], 'criterion_id': key,
                            'judge_verdict': 'violated', 'server_verdict': 'compliant', 'resolution': 'retry_judge',
                            'reason': '서버가 확인한 허용 내용 자체는 의미 위반의 증거가 아님',
                            'evidence': quote, 'evidence_path': path, 'judge_reason': value['reason']})
        elif quote or path:
            kind = 'unexpected_evidence'
        if kind:
            failures.append({'rule': specs[key]['rule'], 'criterion_id': key, 'field': path,
                             'failure_kind': kind, 'rejected_evidence': quote})
        checked[key] = {**value, 'rule': specs[key]['rule'], 'authority': 'judge'}
    validation.update(evidence_required=required, evidence_valid=not failures if required else None,
                      failed_rules=failures, valid=not failures)
    if failures:
        validation.update(failure_kind=failures[0]['failure_kind'], error='Judge evidence or authority validation failed')
        return None, validation
    return summarize(checked), validation


def resolve(lex, judge, execution, validation):
    from .evaluation_v2 import combine
    checks = deepcopy(execution['checks'])
    reasons = set()
    if validation.get('valid'):
        for rule in judge.values():
            for c in rule.get('criteria', []):
                checks[c['criterion_id']] = {k: v for k, v in c.items() if k != 'criterion_id'}
                if c['verdict'] == 'uncertain':
                    reasons.add('semantic_uncertainty')
    else:
        reasons.add('judge_error')
        for rule in ('INFO', 'SCOPE', 'EXEC'):
            checks['judge_error_' + rule] = {'verdict': 'uncertain', 'rule': rule, 'authority': 'validation',
                                           'reason': validation.get('error') or 'Judge 검증 실패'}
    if any(c['verdict'] == 'uncertain' for c in execution['checks'].values()):
        reasons.add('server_check_uncertainty')
    if validation.get('authority_conflicts') and not validation.get('valid'):
        reasons.add('judgment_conflict')
    effective = summarize(checks)
    verdict = combine([lex['verdict'], *[v['verdict'] for v in effective.values()]])
    conflicts = [{**c, 'resolved_after_validation': validation.get('valid') is True}
                 for attempt in validation.get('attempts', [validation]) for c in attempt.get('authority_conflicts', [])]
    return {'schema_version': 2, 'conflict_checked': True, 'effective_rules': effective,
            'criteria': checks, 'conflicts': conflicts,
            'uncertainty_reasons': sorted(reasons), 'aggregate_verdict': verdict,
            'history_eligible': validation.get('valid') is True and verdict == 'compliant',
            'output_contract': execution['output']['contract']}
