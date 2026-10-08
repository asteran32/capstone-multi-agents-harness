"""Immutable evaluation corpus and human review, independent of historical runs."""
from copy import deepcopy
from sqlalchemy import select, func

from .config import settings
from .contracts import digest, parse_policy, evaluation_version, JudgeReplaySettings, ROLES
from .db import (ReviewSet, ReviewCase, Reevaluation, ReevaluationResult, ReevaluationRequest,
                 Batch, Run, Call, Response, Snapshot, ToolExecution, Evaluation, Job, uid, now, record)
from .evaluation_v2 import evaluation_profile
from .services import Conflict, DEFAULT_POLICY
from .validation_cases import validation_cases
from .output_contracts import contract_for


def create_review_set(s, batch, body, key):
    # Keep historical idempotency keys valid for the original validation mode.
    fields = body.model_dump(mode='json', exclude={'mode'} if body.mode == 'labeled_validation' else set())
    request_hash = digest({'batch_id': batch.id, **fields})
    existing = s.scalar(select(ReviewSet).where(ReviewSet.idempotency_key == key))
    if existing:
        if existing.request_sha256 != request_hash:
            raise Conflict('같은 요청 키에 다른 검증 세트가 있습니다.')
        return existing
    ids = [i['run_id'] for i in batch.manifest]
    if s.scalar(select(Job.id).where(Job.run_id.in_(ids), Job.job_type.in_(['run', 'offline']),
                                   Job.status.in_(['queued', 'running'])).limit(1)):
        raise Conflict('배치 생성·사후 평가가 종료된 후 검증 세트를 준비하세요.')
    runs = {r.id: r for r in s.scalars(select(Run).where(Run.id.in_(ids)))}
    if not runs:
        raise ValueError('배치에 Run이 없습니다.')
    original = runs[ids[0]].config_snapshot
    if any(digest(r.config_snapshot['policy']) != digest(original['policy']) for r in runs.values()):
        raise ValueError('역할 정책이 동일한 배치가 필요합니다.')
    if body.judge_model not in ('mock', *settings.live_models):
        raise ValueError('서버에 등록되지 않은 Judge 모델입니다.')
    if body.mode == 'judge_replay' and body.policy_raw is not None:
        raise ValueError('응답 고정 시 정책을 수정할 수 없습니다. Judge 실행 설정에서 정책을 선택하세요.')
    raw = body.policy_raw if body.policy_raw is not None else original['policy_raw']
    policy = parse_policy(raw).model_dump()
    if body.mode == 'judge_replay':
        # The corpus stores source settings; each replay separately freezes its evaluator.
        config = {'mode': 'judge_replay', 'policy_raw': raw, 'policy': policy,
                  'judge_model': original['judge_model'], 'budget': deepcopy(original['budget']),
                  'source_evaluation_version': original['evaluation_version']}
    else:
        profile = evaluation_profile(policy)
        config = {**fields, 'policy_raw': raw, 'policy': policy,
                  'evaluation_profile': profile, 'transport_attempts': 2, 'judge_attempts': 2,
                  'policy_changed': digest(policy) != digest(original['policy']),
                  'evaluation_version': evaluation_version(policy, body.judge_model, body.budget.max_output_tokens, profile)}
    review_set = ReviewSet(id=uid(), batch_id=batch.id, idempotency_key=key,
                          request_sha256=request_hash, corpus_sha256='', config_snapshot=config)
    s.add(review_set)
    s.flush()
    rows = s.execute(select(Response, Call).join(Call, Call.id == Response.call_id)
                     .where(Call.run_id.in_(ids))).all()
    rows.sort(key=lambda pair: (ids.index(pair[1].run_id), pair[1].call_seq, pair[0].response_version))
    if not rows:
        raise ValueError('저장된 응답이 없습니다.')
    corpus = []
    for response, call in rows:
        config_original = runs[call.run_id].config_snapshot
        snapshot = s.scalar(select(Snapshot).where(Snapshot.call_id == call.id,
                            Snapshot.response_version == response.response_version))
        if not snapshot or digest(response.raw_text) != response.response_sha256:
            raise ValueError('원본 응답 해시 또는 입력 스냅샷을 확인할 수 없습니다.')
        snapshot_content = {k: getattr(snapshot, k) for k in ('role_prompt', 'task_input', 'ordered_history')}
        if snapshot.recovery_feedback:
            snapshot_content['recovery_feedback'] = snapshot.recovery_feedback
        if digest(snapshot_content) != snapshot.content_sha256:
            raise ValueError('원본 입력 스냅샷 해시가 일치하지 않습니다.')
        tool = s.scalar(select(ToolExecution).where(ToolExecution.call_id == call.id,
                                                   ToolExecution.operation_key == 'original-tests'))
        phase = 'offline' if runs[call.run_id].mode in ('Base', 'L1') else 'online'
        baseline = s.scalar(select(Evaluation).where(Evaluation.response_id == response.id,
            Evaluation.phase == phase, Evaluation.evaluation_version == config_original['evaluation_version']))
        data = {'source_task_id': config_original['source_task_id'], 'run_id': call.run_id,
                'call_id': call.id, 'round_index': call.round_index, 'response_id': response.id,
                'response_version': response.response_version, 'raw_text': response.raw_text,
                'response_sha256': response.response_sha256, 'snapshot_id': snapshot.id,
                'snapshot_sha256': snapshot.content_sha256, 'task_input': snapshot.task_input,
                'role_prompt': snapshot.role_prompt, 'ordered_history': snapshot.ordered_history,
                'output_contract': contract_for(next(p for p in config_original['policy']['roles'] if p['role'] == call.role)),
                'recovery_feedback': snapshot.recovery_feedback, 'user_prompt': call.user_prompt,
                'tool': {k: getattr(tool, k) for k in ('id', 'status', 'arguments', 'result', 'stdout', 'stderr', 'junit_xml')} if tool else None,
                'baseline': {k: v for k, v in record(baseline).items() if k != 'created_at'} if baseline else None}
        corpus.append({'case_key': response.id, 'kind': 'stored', 'role': call.role, 'data': data, 'label': {'status': 'unreviewed'}})
    if body.mode == 'labeled_validation':
        corpus.extend({**c, 'kind': 'synthetic'} for c in validation_cases(policy))
    else:
        review_set.status = 'ready'
    for position, item in enumerate(corpus):
        item_hash = digest({'case_key': item['case_key'], 'kind': item['kind'], 'role': item['role'], 'data': item['data']})
        s.add(ReviewCase(review_set_id=review_set.id, position=position, data_sha256=item_hash, **item))
    review_set.corpus_sha256 = digest([digest({'case_key': c['case_key'], 'kind': c['kind'], 'role': c['role'], 'data': c['data']}) for c in corpus])
    s.flush()
    return review_set


def cases_for(s, set_id):
    return list(s.scalars(select(ReviewCase).where(ReviewCase.review_set_id == set_id).order_by(ReviewCase.position)))


def check_corpus(review_set, cases):
    hashes = [digest({k: getattr(c, k) for k in ('case_key', 'kind', 'role', 'data')}) for c in cases]
    if hashes != [c.data_sha256 for c in cases] or digest(hashes) != review_set.corpus_sha256:
        raise ValueError('고정 검증 세트 해시가 일치하지 않습니다.')


def is_replay(review_set):
    return review_set.config_snapshot.get('mode') == 'judge_replay'


def replay_preview(s, review_set, body):
    if not is_replay(review_set):
        raise Conflict('Judge 전용 응답 세트가 필요합니다.')
    cases = cases_for(s, review_set.id)
    check_corpus(review_set, cases)
    if body.judge_model not in ('mock', *settings.live_models):
        raise ValueError('서버에 등록되지 않은 Judge 모델입니다.')
    if body.budget.max_requests < len(cases):
        raise ValueError('전체 요청 한도는 고정 응답 수 이상이어야 합니다.')
    source = review_set.config_snapshot
    try:
        raw = DEFAULT_POLICY.read_text() if body.policy_source == 'current' else source['policy_raw']
    except OSError as exc:
        raise ValueError('현재 역할 정책 파일을 읽을 수 없습니다.') from exc
    policy = parse_policy(raw).model_dump()
    profile = evaluation_profile(policy)
    config = {**JudgeReplaySettings.model_validate(body.model_dump(include=set(JudgeReplaySettings.model_fields))).model_dump(mode='json'),
              'mode': 'judge_replay', 'policy_raw': raw, 'policy': policy,
              'evaluation_profile': profile, 'transport_attempts': 2, 'judge_attempts': 2,
              'corpus_sha256': review_set.corpus_sha256, 'source_policy_version': source['policy']['version'],
              'policy_changed': digest(policy) != digest(source['policy']),
              'policy_sha256': digest(policy), 'judge_profile_sha256': digest(profile),
              'evaluation_version': evaluation_version(policy, body.judge_model, body.budget.max_output_tokens, profile)}
    return {'config_snapshot': config, 'config_sha256': digest(config), 'total': len(cases),
            'roles': {role: sum(c.role == role for c in cases) for role in ROLES},
            'base_requests': len(cases), 'max_requests': min(len(cases) * 4, body.budget.max_requests)}


def start_replay(s, review_set, body, key):
    if not is_replay(review_set):
        raise Conflict('Judge 전용 응답 세트가 필요합니다.')
    request_hash = digest({'review_set_id': review_set.id, **body.model_dump(mode='json')})
    existing = s.scalar(select(Reevaluation).where(Reevaluation.idempotency_key == key))
    if existing:
        if existing.request_sha256 != request_hash:
            raise Conflict('같은 요청 키에 다른 재평가 실행이 있습니다.')
        return existing
    for id in s.scalars(select(Reevaluation.id).where(Reevaluation.review_set_id == review_set.id)):
        job = execution_job(s, id)
        if job and job.status in ('queued', 'running'):
            raise Conflict('이 응답 세트의 Judge 실행이 진행 중입니다.')
    preview = replay_preview(s, review_set, body)
    if preview['config_sha256'] != body.expected_config_sha256:
        raise Conflict('정책 또는 Judge 설정이 변경되었습니다. 실행 설정을 다시 확인하세요.')
    config = {**preview['config_snapshot'], 'config_sha256': preview['config_sha256']}
    return queue_reevaluation(s, review_set, key, request_hash, config, {}, cases_for(s, review_set.id))


def review_summary(s, review_set, detail=False):
    cases = cases_for(s, review_set.id)
    result = {**record(review_set), 'mode': review_set.config_snapshot.get('mode', 'labeled_validation'), 'total': len(cases),
              'roles': {role: sum(c.role == role for c in cases) for role in ROLES},
              'versions': {str(v): sum(c.data['response_version'] == v for c in cases)
                           for v in sorted({c.data['response_version'] for c in cases})},
              'stored': sum(c.kind == 'stored' for c in cases),
              'synthetic': sum(c.kind == 'synthetic' for c in cases),
              'reviewed': sum(c.label.get('status') == 'reviewed' for c in cases)}
    if detail:
        result['cases'] = [record(c) for c in cases]
        result['executions'] = [execution_summary(s, r) for r in s.scalars(select(Reevaluation)
            .where(Reevaluation.review_set_id == review_set.id).order_by(Reevaluation.created_at.desc()))]
    return result


def update_label(s, review_set, case, body):
    if review_set.status != 'draft':
        raise Conflict('확정된 검토는 변경할 수 없습니다. 새 검증 세트를 준비하세요.')
    if review_set.revision != body.revision:
        raise Conflict('다른 검토가 저장되었습니다. 새로고침하세요.')
    previous = {k: v for k, v in case.label.items() if k != 'history'}
    case.label = {'status': 'reviewed', 'verdict': body.verdict, 'reason': body.reason,
                  'reviewer': body.reviewer, 'reviewed_at': now().isoformat(),
                  'history': [*case.label.get('history', []), previous]}
    review_set.revision += 1
    s.flush()


def seal_review(s, review_set, body):
    if is_replay(review_set):
        raise Conflict('Judge 전용 세트에는 사람 기대 판정을 확정하지 않습니다.')
    if review_set.status == 'ready':
        return
    if review_set.revision != body.revision:
        raise Conflict('검토 내용이 변경되었습니다. 새로고침하세요.')
    cases = cases_for(s, review_set.id)
    check_corpus(review_set, cases)
    if not body.reviewer.strip() or any(c.label.get('status') != 'reviewed' for c in cases):
        raise Conflict('모든 기대 판정을 검토하고 정책 확인자를 입력하세요.')
    review_set.review = {'reviewer': body.reviewer.strip(), 'policy_confirmed': True,
                        'sealed_at': now().isoformat(), 'labels_sha256': digest({c.id: c.label for c in cases})}
    review_set.status = 'ready'
    review_set.revision += 1


def start_reevaluation(s, review_set, key):
    if is_replay(review_set):
        raise Conflict('Judge 실행 설정을 확인한 뒤 전용 실행 경로를 사용하세요.')
    request_hash = digest({'review_set_id': review_set.id})
    existing = s.scalar(select(Reevaluation).where(Reevaluation.idempotency_key == key))
    if existing:
        if existing.request_sha256 != request_hash:
            raise Conflict('같은 요청 키에 다른 재평가 실행이 있습니다.')
        return existing
    if review_set.status != 'ready':
        raise Conflict('정책과 모든 기대 판정을 검토·확정한 후 실행하세요.')
    cases = cases_for(s, review_set.id)
    check_corpus(review_set, cases)
    labels = {c.id: c.label for c in cases}
    if digest(labels) != review_set.review['labels_sha256']:
        raise Conflict('확정된 기대 판정이 변경되었습니다.')
    return queue_reevaluation(s, review_set, key, request_hash, review_set.config_snapshot, labels, cases)


def queue_reevaluation(s, review_set, key, request_hash, config, labels, cases):
    run = Reevaluation(id=uid(), review_set_id=review_set.id, idempotency_key=key, request_sha256=request_hash,
                      config_snapshot=deepcopy(config), labels_snapshot=labels)
    s.add(run)
    s.flush()
    for case in cases:
        s.add(ReevaluationResult(reevaluation_id=run.id, case_id=case.id))
    batch = s.get(Batch, review_set.batch_id)
    s.add(Job(run_id=batch.manifest[0]['run_id'], job_type='reevaluation', job_key=f'reevaluation:{run.id}',
              payload={'reevaluation_id': run.id}))
    s.flush()
    return run


def execution_job(s, id):
    return s.scalar(select(Job).where(Job.job_key == f'reevaluation:{id}'))


def execution_summary(s, run, detail=False):
    job = execution_job(s, run.id)
    results = list(s.scalars(select(ReevaluationResult).where(ReevaluationResult.reevaluation_id == run.id)))
    counts = dict(s.execute(select(ReevaluationRequest.status, func.count()).where(ReevaluationRequest.reevaluation_id == run.id)
                            .group_by(ReevaluationRequest.status)).all())
    result = {**record(run), 'status': job.status, 'error': job.error, 'total': len(results),
              'finished': sum(r.status in ('completed', 'error') for r in results),
              'errors': sum(r.status == 'error' for r in results), 'requests': sum(counts.values()),
              'request_statuses': counts}
    if detail:
        from .reevaluation_metrics import validation_metrics, replay_metrics, request_usage
        review_set = s.get(ReviewSet, run.review_set_id)
        cases = cases_for(s, review_set.id)
        result['results'] = [record(r) for r in results]
        result['metrics'] = replay_metrics(run, cases, results) if is_replay(review_set) else validation_metrics(run, cases, results)
        result['usage'] = request_usage(list(s.scalars(select(ReevaluationRequest).where(ReevaluationRequest.reevaluation_id == run.id))))
    return result


def control_execution(s, run, action):
    job = execution_job(s, run.id)
    if action == 'cancel':
        if job.status in ('queued', 'running'):
            run.cancel_requested = True
            if job.status == 'queued':
                job.status = 'cancelled'
    elif action == 'resume':
        if job.status not in ('failed', 'cancelled'):
            raise Conflict('실패하거나 취소된 작업만 재개할 수 있습니다.')
        if run.config_snapshot.get('mode') == 'judge_replay':
            for other_id in s.scalars(select(Reevaluation.id).where(
                    Reevaluation.review_set_id == run.review_set_id, Reevaluation.id != run.id)):
                other_job = execution_job(s, other_id)
                if other_job and other_job.status in ('queued', 'running'):
                    raise Conflict('이 응답 세트의 다른 Judge 실행이 진행 중입니다.')
        run.cancel_requested = False
        job.status, job.error, job.lease_owner, job.lease_expires_at = 'queued', None, None, None
    s.flush()
    return execution_summary(s, run)
