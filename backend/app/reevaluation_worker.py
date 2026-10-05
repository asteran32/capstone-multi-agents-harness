"""Judge-only durable execution; never generates responses, runs tools, or repairs agents."""
from sqlalchemy import select, func
from . import runtime
from .contracts import canonical, digest, evaluation_version
from .db import (transaction, Reevaluation, ReviewSet, ReviewCase, ReevaluationResult, ReevaluationRequest)
from .evaluation import lexical, check_judge, uncertain
from .evaluation_v2 import judge_payload, runner_checks, resolve_judgment
from .providers import respond, ProviderError
from .runtime import Cancelled, BudgetExceeded
from .reevaluation import cases_for, check_corpus


def check_cancel(id):
    runtime.lease_check()
    with transaction() as s:
        if s.get(Reevaluation, id).cancel_requested:
            raise Cancelled('재평가 취소 요청')


def request_judge(run_id, result_id, validation_attempt, config, payload, data):
    check_cancel(run_id)
    if len(canonical(payload).encode()) > config['budget']['max_input_bytes']:
        raise BudgetExceeded('context_limit: 고정 입력을 삭제하거나 요약하지 않았습니다.')
    with transaction() as s:
        rows = list(s.scalars(select(ReevaluationRequest).where(ReevaluationRequest.result_id == result_id,
                    ReevaluationRequest.validation_attempt == validation_attempt).order_by(ReevaluationRequest.transport_attempt)))
        done = next((r for r in rows if r.status == 'completed'), None)
        if done:
            return done.response_payload
        for row in rows:
            if row.status == 'running':
                row.status = 'unknown'
                row.error = {'message': '응답 저장 전 중단: 공급자 중복 호출 가능', 'retryable': True}
        if rows and rows[-1].status == 'failed' and not (rows[-1].error or {}).get('retryable'):
            raise ProviderError(rows[-1].error['message'], rows[-1].response_payload)
        start = len(rows)
    for attempt in range(start + 1, config['transport_attempts'] + 1):
        check_cancel(run_id)
        with transaction() as s:
            count = s.scalar(select(func.count()).select_from(ReevaluationRequest)
                             .where(ReevaluationRequest.reevaluation_id == run_id))
            if count >= config['budget']['max_requests']:
                raise BudgetExceeded('재평가 전용 요청 예산 소진')
            request = ReevaluationRequest(reevaluation_id=run_id, result_id=result_id,
                validation_attempt=validation_attempt, transport_attempt=attempt,
                request_payload=payload, status='running')
            s.add(request)
            s.flush()
            request_id = request.id
        try:
            raw = respond(payload, {'kind': 'judge', 'data': data})
        except ProviderError as exc:
            with transaction() as s:
                request = s.get(ReevaluationRequest, request_id)
                request.status, request.response_payload = 'failed', exc.raw
                request.error = {'message': str(exc), 'retryable': exc.retryable}
            if not exc.retryable:
                raise
        else:
            runtime.lease_check()
            with transaction() as s:
                request = s.get(ReevaluationRequest, request_id)
                request.status, request.response_payload = 'completed', raw
            return raw
    raise ProviderError('Transport attempts exhausted')


def evaluate_case(run_id, result_id):
    check_cancel(run_id)
    with transaction() as s:
        run, row = s.get(Reevaluation, run_id), s.get(ReevaluationResult, result_id)
        if row.status in ('completed', 'error'):
            return
        case, config = s.get(ReviewCase, row.case_id), run.config_snapshot
        data, role = case.data, case.role
        result = dict(row.result)
        result.setdefault('lexical_result', lexical(data['raw_text'], config['policy']['forbidden_terms']))
        result.setdefault('execution_evidence', runner_checks(role, data['raw_text'], data['task_input'], data.get('tool')))
        result.setdefault('validation_attempts', [])
        row.result, row.status = result, 'running'
    attempts = result['validation_attempts']
    while len(attempts) < config['judge_attempts'] and not (attempts and (attempts[-1]['valid'] or attempts[-1].get('failure_kind') in ('provider', 'budget'))):
        check_cancel(run_id)
        payload, judge_data = judge_payload(config, role, data.get('response_id') or case.id,
            data['response_sha256'], data['raw_text'], data['task_input'], data['user_prompt'],
            result['execution_evidence'], attempts[-1] if attempts else None)
        raw_text = ''
        try:
            raw = request_judge(run_id, result_id, len(attempts) + 1, config, payload, judge_data)
            if raw.get('status') != 'completed':
                raise ProviderError('Incomplete Judge response', raw)
            raw_text = ''.join(part.get('text', '') for item in raw.get('output', []) if item.get('type') == 'message'
                               for part in item.get('content', []) if part.get('type') == 'output_text')
            judge, validation = check_judge(raw_text, data['raw_text'], role, v2=True)
        except (ProviderError, BudgetExceeded) as exc:
            judge = None
            validation = {'valid': False, 'schema_valid': None, 'evidence_valid': None,
                          'evidence_required': None, 'failed_rules': [], 'error': str(exc),
                          'failure_kind': 'budget' if isinstance(exc, BudgetExceeded) else 'provider'}
        validation = {**validation, 'schema_version': 3, 'attempt': len(attempts) + 1, 'judge_output': raw_text}
        attempts = [*attempts, validation]
        result = {**result, 'validation_attempts': attempts,
                  'judge_result': judge if validation['valid'] else uncertain(validation['error'] or 'Judge failed')}
        with transaction() as s:
            s.get(ReevaluationResult, result_id).result = result
    check_cancel(run_id)
    resolution = resolve_judgment(result['lexical_result'], result['judge_result'], result['execution_evidence'], attempts[-1]['valid'])
    with transaction() as s:
        row = s.get(ReevaluationResult, result_id)
        row.result = {**result, 'judgment_resolution': resolution, 'aggregate_verdict': resolution['aggregate_verdict']}
        row.status = 'completed' if attempts[-1]['valid'] else 'error'


def process_reevaluation(id):
    check_cancel(id)
    with transaction() as s:
        run = s.get(Reevaluation, id)
        review_set = s.get(ReviewSet, run.review_set_id)
        cases = cases_for(s, review_set.id)
        check_corpus(review_set, cases)
        if digest(run.labels_snapshot) != review_set.review['labels_sha256']:
            raise ValueError('확정 기대 판정 해시 불일치')
        config = run.config_snapshot
        if config['evaluation_version'] != evaluation_version(config['policy'], config['judge_model'],
                config['budget']['max_output_tokens'], config['evaluation_profile']):
            raise ValueError('평가 버전 해시 불일치')
        ids = list(s.scalars(select(ReevaluationResult.id).join(ReviewCase, ReviewCase.id == ReevaluationResult.case_id)
            .where(ReevaluationResult.reevaluation_id == id).order_by(ReviewCase.position)))
    for result_id in ids:
        evaluate_case(id, result_id)
