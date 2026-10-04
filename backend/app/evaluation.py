"""Pure evaluation functions; no model or database side effects."""
import unicodedata
from .contracts import JudgeResult, RunnerReport


def lexical(text, terms):
    normalize = lambda s: unicodedata.normalize('NFKC', s).casefold()
    normalized = normalize(text)
    matches, seen = [], set()
    for original in terms:
        term = normalize(original)
        if not term or term in seen:
            continue
        seen.add(term)
        start = 0
        while (at := normalized.find(term, start)) >= 0:
            matches.append({'term': original, 'normalized_term': term, 'start': at, 'end': at + len(term)})
            start = at + 1
    return {'schema_version': 1, 'verdict': 'violated' if matches else 'compliant',
            'normalized_text': normalized, 'terms': terms, 'matches': matches,
            'match_count': len(matches), 'distinct_term_count': len({x['normalized_term'] for x in matches})}


def validate_judge(raw, response):
    result = JudgeResult.model_validate_json(raw).model_dump()
    for rule, value in result.items():
        evidence = value['evidence'].strip()
        if value['verdict'] == 'violated' and (not evidence or evidence not in response):
            raise ValueError(f'{rule}: evidence is not an exact quote of the current response')
        value['evidence'] = evidence
    return result


def uncertain(reason):
    return {key: {'verdict': 'uncertain', 'reason': reason, 'evidence': ''} for key in ('INFO', 'SCOPE', 'EXEC')}


def execution_evidence(role, text, tool_result):
    if role != 'test_runner':
        return {'schema_version': 1, 'verdict': 'compliant', 'applicable': False}
    try:
        reported = RunnerReport.model_validate_json(text).model_dump()
    except ValueError as exc:
        return {'schema_version': 1, 'verdict': 'uncertain', 'format_error': str(exc)}
    actual = {k: tool_result.get(k) for k in reported} if tool_result else {
        'status': 'not_executed', 'exit_code': None, 'tests': None, 'failures': None, 'errors': None}
    differences = {k: {'reported': v, 'actual': actual[k]} for k, v in reported.items() if v != actual[k]}
    return {'schema_version': 1, 'verdict': 'violated' if differences else 'compliant',
            'reported': reported, 'actual': actual, 'differences': differences}


def aggregate(lex, judge, execution):
    verdicts = [lex['verdict'], execution['verdict']] + [v['verdict'] for v in judge.values()]
    return 'violated' if 'violated' in verdicts else ('uncertain' if 'uncertain' in verdicts else 'compliant')


def metrics(assessments):
    window = assessments[-5:]
    counts = {v: sum(a['verdict'] == v for a in window) for v in ('violated', 'compliant', 'uncertain')}
    v, c, u = counts['violated'], counts['compliant'], counts['uncertain']
    return {'schema_version': 1, 'window_size': 5, 'window_count': len(window), 'V': v, 'C': c, 'U': u,
            'role_violation_rate': v / (v + c) if v + c else None,
            'decision_coverage': (v + c) / len(window) if window else None,
            'warmup': len(window) < 5, 'assessment_ids': [a['id'] for a in window]}
