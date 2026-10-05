"""Explicit denominators and conservative promotion criteria for reviewed cases."""
from collections import Counter
from .contracts import ROLES


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
