"""Dashboard endpoints. Preparation/review never queues provider requests."""
import csv
import io
from uuid import UUID
from typing import Literal
import yaml
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response as HTTPResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .contracts import ReviewSetCreate, ReviewLabel, ReviewSeal, ComparisonCreate, BatchCreate, digest
from .db import transaction, ReviewSet, ReviewCase, Reevaluation, ReevaluationResult, ReevaluationRequest, Batch, record
from .reevaluation import (create_review_set, review_summary, update_label, seal_review, start_reevaluation,
                           execution_summary, control_execution, cases_for)
from .services import Conflict
from .batches import create_batch
from .exports import csv_cell


def router(authenticate, require, key_required):
    routes = APIRouter(prefix='/api', dependencies=[Depends(authenticate)])

    def lock(s, model, id):
        row = s.scalar(select(model).where(model.id == str(id)).with_for_update())
        if not row:
            raise HTTPException(404, '기록을 찾을 수 없습니다.')
        return row

    def mutation(fn):
        try:
            return fn()
        except Conflict as exc:
            raise HTTPException(409, str(exc))
        except IntegrityError:
            raise HTTPException(409, '동시 요청 충돌입니다. 같은 요청 키로 다시 시도하세요.')
        except (ValueError, yaml.YAMLError) as exc:
            raise HTTPException(422, str(exc))

    @routes.get('/batches/{id}/review-sets')
    def sets(id: UUID):
        with transaction() as s:
            require(s, Batch, id)
            return [review_summary(s, r) for r in s.scalars(select(ReviewSet).where(ReviewSet.batch_id == str(id))
                                                          .order_by(ReviewSet.created_at.desc()))]

    @routes.post('/batches/{id}/review-sets', status_code=201)
    def prepare(id: UUID, body: ReviewSetCreate, idempotency_key: str | None = Header(default=None)):
        key = key_required(idempotency_key)
        def operation():
            with transaction() as s:
                return review_summary(s, create_review_set(s, require(s, Batch, id), body, key), detail=True)
        return mutation(operation)

    @routes.get('/review-sets/{id}')
    def detail(id: UUID):
        with transaction() as s:
            return review_summary(s, require(s, ReviewSet, id), detail=True)

    @routes.put('/review-sets/{id}/cases/{case_id}/label')
    def label(id: UUID, case_id: UUID, body: ReviewLabel):
        def operation():
            with transaction() as s:
                review_set, case = lock(s, ReviewSet, id), require(s, ReviewCase, case_id)
                if case.review_set_id != review_set.id:
                    raise HTTPException(404, '검증 세트의 사례가 아닙니다.')
                update_label(s, review_set, case, body)
                return review_summary(s, review_set, detail=True)
        return mutation(operation)

    @routes.post('/review-sets/{id}/seal')
    def seal(id: UUID, body: ReviewSeal):
        def operation():
            with transaction() as s:
                review_set = lock(s, ReviewSet, id)
                seal_review(s, review_set, body)
                return review_summary(s, review_set, detail=True)
        return mutation(operation)

    @routes.post('/review-sets/{id}/execute', status_code=202)
    def execute(id: UUID, idempotency_key: str | None = Header(default=None)):
        key = key_required(idempotency_key)
        def operation():
            with transaction() as s:
                return execution_summary(s, start_reevaluation(s, lock(s, ReviewSet, id), key))
        return mutation(operation)

    @routes.get('/reevaluations/{id}')
    def execution(id: UUID):
        with transaction() as s:
            return execution_summary(s, require(s, Reevaluation, id), detail=True)

    @routes.get('/reevaluations/{id}/cases/{case_id}')
    def audit(id: UUID, case_id: UUID):
        with transaction() as s:
            run = require(s, Reevaluation, id)
            case = require(s, ReviewCase, case_id)
            if case.review_set_id != run.review_set_id:
                raise HTTPException(404, '재평가 사례가 아닙니다.')
            result = s.scalar(select(ReevaluationResult).where(ReevaluationResult.reevaluation_id == run.id,
                                                             ReevaluationResult.case_id == case.id))
            return {'case': record(case), 'result': record(result), 'label': run.labels_snapshot[case.id],
                    'requests': [record(r) for r in s.scalars(select(ReevaluationRequest)
                        .where(ReevaluationRequest.result_id == result.id)
                        .order_by(ReevaluationRequest.validation_attempt, ReevaluationRequest.transport_attempt))]}

    @routes.post('/reevaluations/{id}/{action}')
    def control(id: UUID, action: Literal['cancel', 'resume']):
        def operation():
            with transaction() as s:
                return control_execution(s, lock(s, Reevaluation, id), action)
        return mutation(operation)

    @routes.get('/reevaluations/{id}/results.csv')
    def export(id: UUID):
        with transaction() as s:
            from sqlalchemy import text
            if s.get_bind().dialect.name == 'postgresql':
                s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            run = require(s, Reevaluation, id)
            detail = execution_summary(s, run, detail=True)
            review_set = s.get(ReviewSet, run.review_set_id)
            results = {r['case_id']: r for r in detail['results']}
            rows = []
            for case in cases_for(s, review_set.id):
                row, label, baseline = results[case.id], run.labels_snapshot[case.id], case.data.get('baseline') or {}
                rows.append({'csv_schema_version': 1, 'reevaluation_id': run.id, 'review_set_id': review_set.id,
                    'corpus_sha256': review_set.corpus_sha256, 'evaluation_version': run.config_snapshot['evaluation_version'],
                    'judge_model': run.config_snapshot['judge_model'], 'policy_changed': run.config_snapshot['policy_changed'],
                    'case_id': case.id, 'kind': case.kind, 'role': case.role, 'source_task_id': case.data['source_task_id'],
                    'response_id': case.data.get('response_id'), 'response_version': case.data['response_version'],
                    'response_sha256': case.data['response_sha256'], 'response': case.data['raw_text'],
                    'expected': label['verdict'], 'reviewer': label['reviewer'], 'reason': label['reason'],
                    'baseline_version': baseline.get('evaluation_version'), 'baseline_status': baseline.get('status'),
                    'baseline_verdict': baseline.get('aggregate_verdict'), 'new_status': row['status'],
                    'new_verdict': row['result'].get('aggregate_verdict'), 'audit': row['result']})
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: csv_cell(v) for k, v in row.items()})
        return HTTPResponse(('\ufeff' + output.getvalue()).encode('utf-8'), media_type='text/csv; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="guardrail-reevaluation-{id}.csv"'})

    # Register a fixed path before the generic /{action} route is considered.
    @routes.post('/reevaluations/{id}/comparisons/start', status_code=202)
    def compare(id: UUID, body: ComparisonCreate, idempotency_key: str | None = Header(default=None)):
        key = key_required(idempotency_key)
        def operation():
            with transaction() as s:
                run = lock(s, Reevaluation, id)
                request_hash = digest(body.model_dump(mode='json'))
                previous = next((x for x in run.comparison_batches if x['key'] == key), None)
                if previous:
                    if previous['request_sha256'] != request_hash:
                        raise Conflict('같은 요청 키에 다른 비교 조건이 있습니다.')
                    return previous
                detail = execution_summary(s, run, detail=True)
                if detail['status'] != 'completed' or not detail['metrics']['promotion']['ready']:
                    raise Conflict('실제 모델 검증을 통과한 완료 결과가 필요합니다.')
                review_set = s.get(ReviewSet, run.review_set_id)
                source, evaluator = s.get(Batch, review_set.batch_id), run.config_snapshot
                items = []
                for rounds in body.rounds:
                    for mode in ('L1', 'L1+L2', 'FULL'):
                        for strategy in body.recovery_strategies if mode == 'FULL' else ['feedback']:
                            config = {**source.config_snapshot, 'mode': mode, 'rounds': rounds, 'evaluate_l1': True,
                                      'policy_raw': evaluator['policy_raw'], 'judge_model': evaluator['judge_model'],
                                      'budget': {**source.config_snapshot['budget'],
                                          'max_input_bytes': evaluator['budget']['max_input_bytes'],
                                          'max_output_tokens': evaluator['budget']['max_output_tokens']}}
                            batch = create_batch(s, BatchCreate(**config), f'comparison:{run.id}:{digest(key)}:{rounds}:{mode}:{strategy}',
                                                 source, evaluator, strategy)
                            items.append({'batch_id': batch.id, 'mode': mode, 'rounds': rounds,
                                          'recovery_strategy': strategy if mode == 'FULL' else None,
                                          'evaluation_version': evaluator['evaluation_version']})
                group = {'key': key, 'request_sha256': request_hash, 'conditions': body.model_dump(), 'items': items}
                run.comparison_batches = [*run.comparison_batches, group]
                return group
        return mutation(operation)

    return routes
