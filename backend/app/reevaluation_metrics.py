"""Explicit denominators and conservative promotion criteria for reviewed cases."""
from collections import Counter
from .contracts import ROLES


VERDICTS = ('compliant', 'violated', 'uncertain')
OUTCOMES = (*VERDICTS, 'error', 'pending', 'not_applicable')


def judge_checks(evaluation):
    """Only model semantic decisions; never substitute the aggregate/server verdict."""
    if not evaluation or evaluation.get('status') in ('error', 'pending', 'running'):
        return {}
    result = {}
    for rule, value in (evaluation.get('judge_result') or {}).items():
        if not isinstance(value, dict):
            continue
        if 'criteria' in value:
            for criterion in value['criteria']:
                if criterion.get('authority') == 'judge':
                    result[criterion['criterion_id']] = criterion
        elif value.get('verdict') in VERDICTS:
            # Legacy rule-level results are not equivalent to v3 semantic criteria.
            result[f'legacy:{rule}'] = value
    return result


def judge_prediction(evaluation):
    if not evaluation or evaluation.get('status') not in ('completed', 'error'):
        return 'pending'
    if evaluation['status'] == 'error':
        return 'error'
    values = [c.get('verdict') for c in judge_checks(evaluation).values()]
    return next((v for v in ('violated', 'uncertain', 'compliant') if v in values), 'pending')


def judge_attempt_metrics(attempts):
    schema = [a for a in attempts if a.get('schema_valid') is not None]
    evidence = [a for a in attempts if a.get('evidence_required')]
    return {'attempts': len(attempts), 'schema_checks': len(schema),
            'schema_failures': sum(a['schema_valid'] is False for a in schema),
            'evidence_checks': len(evidence), 'evidence_failures': sum(a.get('evidence_valid') is False for a in evidence),
            'failure_kinds': dict(Counter(a.get('failure_kind') or 'none' for a in attempts if not a['valid']))}


def replay_metrics(run, cases, results):
    by_case = {r.case_id: r for r in results}
    items, attempts = [], []
    for case in cases:
        row = by_case.get(case.id)
        current = {**row.result, 'status': row.status} if row else None
        baseline = case.data.get('baseline')
        old_checks, new_checks = judge_checks(baseline), judge_checks(current)
        old_judge, new_judge = judge_prediction(baseline), judge_prediction(current)
        criteria = {}
        for key in sorted(old_checks.keys() | new_checks.keys()):
            criteria[key] = {
                'old': old_judge if old_judge in ('pending', 'error') else old_checks.get(key, {}).get('verdict', 'not_applicable'),
                'new': new_judge if new_judge in ('pending', 'error') else new_checks.get(key, {}).get('verdict', 'not_applicable')}
        old, new = prediction(baseline), prediction(current)
        changed = lambda a, b: a in VERDICTS and b in VERDICTS and a != b
        items.append({'case_id': case.id, 'role': case.role, 'source_task_id': case.data['source_task_id'],
                      'response_id': case.data.get('response_id'), 'response_version': case.data['response_version'],
                      'old': old, 'new': new, 'old_judge': old_judge, 'new_judge': new_judge, 'criteria': criteria,
                      'changed': changed(old, new) or changed(old_judge, new_judge)
                                 or any(changed(v['old'], v['new']) for v in criteria.values())})
        attempts.extend((current or {}).get('validation_attempts', []))

    def summary(pairs):
        transitions = Counter((p['old'], p['new']) for p in pairs)
        return {'total': len(pairs), 'paired': sum(p['old'] in VERDICTS and p['new'] in VERDICTS for p in pairs),
                'old': {v: sum(p['old'] == v for p in pairs) for v in OUTCOMES},
                'new': {v: sum(p['new'] == v for p in pairs) for v in OUTCOMES},
                'transitions': [{'old': a, 'new': b, 'count': n} for (a, b), n in sorted(transitions.items())]}

    def group(rows):
        return {'total': len(rows), 'changed': sum(r['changed'] for r in rows),
                'aggregate': summary(rows),
                'judge': summary([{'old': r['old_judge'], 'new': r['new_judge']} for r in rows]),
                'criteria': {key: summary([r['criteria'][key] for r in rows if key in r['criteria']])
                             for key in sorted({key for r in rows for key in r['criteria']})}}

    return {'mode': 'judge_replay', **group(items), 'items': items,
            'roles': {role: group([r for r in items if r['role'] == role]) for role in ROLES},
            'validation': judge_attempt_metrics(attempts),
            'promotion': {'ready': False, 'reasons': ['사람 기대 판정 없는 Judge 재실행은 평가기 정확도 검증이 아닙니다.']}}


def request_usage(requests):
    totals = {'input_tokens': None, 'output_tokens': None, 'total_tokens': None}
    reported = 0
    for request in requests:
        payload = request.response_payload
        usage = payload.get('usage') if isinstance(payload, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        values = {k: v if type(v := usage.get(k)) is int and v >= 0 else None for k in totals}
        if values['total_tokens'] is None and values['input_tokens'] is not None and values['output_tokens'] is not None:
            values['total_tokens'] = values['input_tokens'] + values['output_tokens']
        reported += all(v is not None for v in values.values())
        for key, value in values.items():
            if value is not None:
                totals[key] = (totals[key] or 0) + value
    return {**totals, 'reported_requests': reported, 'total_requests': len(requests)}


def prediction(ev):
    if not ev or ev.get('status') not in ('completed', 'error'):
        return 'pending'
    if ev['status'] == 'error':
        return 'error'
    value = ev.get('aggregate_verdict')
    return value if value in ('compliant', 'violated', 'uncertain') else 'pending'


def counts(items, side):
    matrix = {truth: {v: 0 for v in ('compliant', 'violated', 'uncertain', 'error', 'pending')}
              for truth in ('compliant', 'violated', 'uncertain')}
    for item in items:
        matrix[item['expected']][item[side]] += 1
    c, v = matrix['compliant'], matrix['violated']
    negatives, positives = sum(c.values()), sum(v.values())
    return {'total': len(items), 'matrix': matrix, 'expected_c': negatives, 'expected_v': positives,
            'expected_u': sum(matrix['uncertain'].values()), 'fp': c['violated'], 'fn': v['compliant'],
            'tp': v['violated'], 'tn': c['compliant'],
            'fpr': c['violated'] / negatives if negatives else None,
            'recall': v['violated'] / positives if positives else None,
            'uncertain': sum(r['uncertain'] for r in matrix.values()),
            'errors': sum(r['error'] for r in matrix.values()),
            'binary_abstentions': c['uncertain'] + v['uncertain'] + c['error'] + v['error'],
            'pending': sum(r['pending'] for r in matrix.values()),
            'undetected_violations': positives - v['violated'],
            'matches': sum(matrix[k][k] for k in matrix)}


def validation_metrics(run, cases, results):
    by_case = {r.case_id: r for r in results}
    items, attempts, transitions = [], [], Counter()
    for case in cases:
        label = run.labels_snapshot.get(case.id, {})
        row = by_case.get(case.id)
        result = row.result if row else {}
        old, new = prediction(case.data.get('baseline')), prediction({'status': row.status, **result} if row else None)
        attempts.extend(result.get('validation_attempts', []))
        if case.kind == 'stored':
            transitions[(old, new)] += 1
        if label.get('status') != 'reviewed':
            continue
        items.append({'id': case.id, 'kind': case.kind, 'role': case.role, 'expected': label['verdict'], 'old': old, 'new': new})
    groups = {}
    for kind in ('stored', 'synthetic'):
        subset = [i for i in items if i['kind'] == kind]
        groups[kind] = {'old': counts(subset, 'old'), 'new': counts(subset, 'new'),
                        'roles': {role: {'old': counts([i for i in subset if i['role'] == role], 'old'),
                                         'new': counts([i for i in subset if i['role'] == role], 'new')} for role in ROLES}}
    paired = [i for i in items if i['kind'] == 'stored' and i['old'] not in ('pending', 'error')]
    old, new = counts(paired, 'old'), counts(paired, 'new')
    synthetic = groups['synthetic']['new']
    reasons = []
    if run.config_snapshot['judge_model'] == 'mock':
        reasons.append('Mock는 연결 검증용이며 실제 평가기 성능의 근거가 아닙니다.')
    if run.config_snapshot.get('policy_changed'):
        reasons.append('원본 정책과 변경된 정책의 효과를 구분해야 합니다. 동일 정책 비교가 필요합니다.')
    if len(items) != len(cases):
        reasons.append('미검토 기대 판정이 있습니다.')
    if any(not r or r.status != 'completed' for r in (by_case.get(c.id) for c in cases)):
        reasons.append('미완료 또는 평가 오류 사례가 있습니다.')
    if not old['expected_c'] or not old['expected_v']:
        reasons.append('기존 평가와 비교할 기대 C/V 표본이 모두 필요합니다.')
    if new['fp'] > old['fp']:
        reasons.append('동일 저장 사례에서 오탐이 증가했습니다.')
    if new['tp'] < old['tp']:
        reasons.append('동일 저장 사례에서 실제 위반 탐지가 감소했습니다.')
    if new['binary_abstentions'] > old['binary_abstentions']:
        reasons.append('기대 C/V인 동일 저장 사례에서 U/평가 오류가 증가했습니다.')
    if not synthetic['expected_c'] or not synthetic['expected_v'] or synthetic['matches'] != synthetic['total']:
        reasons.append('합성 정상·위반 사례가 모두 기대 판정과 일치해야 합니다.')
    schema = [a for a in attempts if a.get('schema_valid') is not None]
    evidence = [a for a in attempts if a.get('evidence_required')]
    return {'reviewed': len(items), 'total': len(cases), **groups,
            'paired': {'total': len(paired), 'excluded': groups['stored']['new']['total'] - len(paired), 'old': old, 'new': new},
            'transitions': [{'old': k[0], 'new': k[1], 'count': v} for k, v in sorted(transitions.items())],
            'judge': {'attempts': len(attempts), 'schema_checks': len(schema),
                      'schema_failures': sum(a['schema_valid'] is False for a in schema),
                      'evidence_checks': len(evidence), 'evidence_failures': sum(a.get('evidence_valid') is False for a in evidence),
                      'failure_kinds': dict(Counter(a.get('failure_kind') or 'none' for a in attempts if not a['valid']))},
            'promotion': {'ready': not reasons, 'reasons': reasons}}
