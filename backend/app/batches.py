"""Persistent batches of independent, ordered runs using the existing job queue."""
from collections import Counter

from sqlalchemy import select, func, case
from .contracts import BatchCreate, RunCreate, TERMINAL, digest
from .db import Batch, Task, Candidate, Run, Call, Job, Evaluation, Response, now, uid
from .services import Conflict, DEFAULT_POLICY, create_run

JOB_TERMINAL = ('completed', 'failed', 'cancelled')


def create_batch(s, body: BatchCreate, key: str, source: Batch | None = None):
    request_hash = digest({'settings': body.model_dump(mode='json'), 'source_batch_id': source.id if source else None})
    existing = s.scalar(select(Batch).where(Batch.idempotency_key == key))
    if existing:
        if existing.creation_request_sha256 != request_hash:
            raise Conflict('같은 idempotency key에 다른 일괄 실행 요청이 있습니다.')
        return existing

    selected = []
    if source:
        for item in source.manifest:
            task, candidate = s.get(Task, item['task_id']), s.get(Candidate, item['candidate_id'])
            if (not task or not candidate or task.source_sha256 != item['task_sha256']
                    or candidate.code_sha256 != item['candidate_sha256']
                    or task.source_version != item['source_version']):
                raise ValueError('원본 일괄 실행의 문제 또는 후보가 변경되었습니다.')
            selected.append((task, candidate))
    else:
        expected = [f'HumanEval/{i}' for i in range(30)]
        tasks = {t.source_task_id: t for t in s.scalars(select(Task).where(Task.source_task_id.in_(expected)))}
        for source_id in expected:
            task = tasks.get(source_id)
            if not task:
                raise ValueError(f'{source_id}가 없습니다. 30문제 seed를 먼저 실행하세요.')
            candidates = list(s.scalars(select(Candidate).where(Candidate.task_id == task.id,
                                                               Candidate.private_label == 'correct')))
            if len(candidates) != 1:
                raise ValueError(f'{source_id}의 정답 후보가 정확히 1개 있어야 합니다.')
            selected.append((task, candidates[0]))
    if len(selected) != 30:
        raise ValueError('일괄 실행에는 정확히 30문제가 필요합니다.')
    for task, candidate in selected:
        if (candidate.task_id != task.id or candidate.private_label != 'correct'
                or digest(candidate.code) != candidate.code_sha256
                or digest(task.source_record) != task.source_sha256):
            raise ValueError('문제·정답 후보의 연결 또는 해시가 일치하지 않습니다.')

    config = body.model_dump(mode='json')
    config['policy_raw'] = body.policy_raw if body.policy_raw is not None else DEFAULT_POLICY.read_text()
    batch = Batch(id=uid(), idempotency_key=key, creation_request_sha256=request_hash,
                  config_snapshot=config, manifest=[], source_batch_id=source.id if source else None,
                  analysis_settings={'m4_threshold': source.analysis_settings.get('m4_threshold'), 'revision': 0}
                      if source else {})
    s.add(batch)
    s.flush()
    manifest, previous_key = [], None
    for index, (task, candidate) in enumerate(selected, 1):
        run_body = RunCreate(**{k: v for k, v in config.items() if k != 'evaluate_l1'},
                             task_id=task.id, candidate_id=candidate.id)
        run = create_run(s, run_body, f'batch:{batch.id}:{index}')
        run.config_snapshot = {**run.config_snapshot, 'batch_id': batch.id, 'batch_index': index}
        job = s.scalar(select(Job).where(Job.run_id == run.id, Job.job_type == 'run'))
        job.payload = {**job.payload, 'batch_id': batch.id, 'depends_on_key': previous_key}
        previous_key = job.job_key
        if body.mode == 'L1' and body.evaluate_l1:
            version = run.config_snapshot['evaluation_version']
            offline = Job(run_id=run.id, job_key=f'offline:{run.id}:{version}', job_type='offline',
                          payload={'batch_id': batch.id, 'depends_on_key': previous_key,
                                   'request_hash': digest({'run_id': run.id, 'evaluation_version': version}),
                                   'evaluation_version': version})
            s.add(offline)
            previous_key = offline.job_key
        manifest.append({'index': index, 'run_id': run.id, 'task_id': task.id,
                         'source_task_id': task.source_task_id, 'task_sha256': task.source_sha256,
                         'source_version': task.source_version, 'candidate_id': candidate.id,
                         'candidate_sha256': candidate.code_sha256})
    batch.manifest = manifest
    s.flush()
    return batch


def batch_summary(s, batch, detail=False):
    ids = [item['run_id'] for item in batch.manifest]
    runs = {r.id: r for r in s.scalars(select(Run).where(Run.id.in_(ids)))}
    jobs = list(s.scalars(select(Job).where(Job.run_id.in_(ids))))
    by_run = {(j.run_id, j.job_type): j for j in jobs}
    counts = {r.run_id: r for r in s.execute(select(
        Call.run_id, func.count().label('total'),
        func.sum(case((Call.status.in_(TERMINAL), 1), else_=0)).label('finished'),
        func.sum(case((Call.status.in_(['failed', 'blocked', 'unresolved']), 1), else_=0)).label('failed')
    ).where(Call.run_id.in_(ids)).group_by(Call.run_id))}
    evaluation_errors = dict(s.execute(select(Call.run_id, func.count())
        .join(Response, Response.call_id == Call.id).join(Evaluation, Evaluation.response_id == Response.id)
        .where(Call.run_id.in_(ids), Evaluation.status == 'error').group_by(Call.run_id)).all())
    items = []
    for item in batch.manifest:
        run = runs[item['run_id']]
        generation = by_run[(run.id, 'run')]
        offline = by_run.get((run.id, 'offline'))
        done = generation.status in JOB_TERMINAL and (not offline or offline.status in JOB_TERMINAL)
        count = counts[run.id]
        has_errors = (run.status == 'failed' or generation.status == 'failed' or count.failed > 0
                      or bool(evaluation_errors.get(run.id)) or bool(offline and offline.status == 'failed')
                      or (not batch.cancel_requested and (run.status == 'cancelled'
                          or generation.status == 'cancelled' or bool(offline and offline.status == 'cancelled'))))
        items.append({**item, 'status': run.status, 'finished': done, 'has_errors': has_errors,
                      'total_calls': count.total, 'finished_calls': count.finished, 'failed_calls': count.failed,
                      'evaluation_errors': evaluation_errors.get(run.id, 0),
                      'offline_status': offline.status if offline else None, 'error': run.error})
    states = Counter(j.status for j in jobs)
    finished = sum(i['finished'] for i in items)
    errors = sum(i['has_errors'] for i in items)
    if batch.cancel_requested:
        status = 'cancel_requested' if states['running'] else 'cancelled'
    elif finished == len(items):
        status = 'completed_with_errors' if errors else 'completed'
    elif states['running'] or any(j.status in JOB_TERMINAL for j in jobs):
        status = 'running'
    else:
        status = 'queued'
    result = {'id': batch.id, 'created_at': batch.created_at, 'mode': batch.config_snapshot['mode'],
              'status': status, 'total_runs': len(items), 'finished_runs': finished, 'error_runs': errors,
              'source_batch_id': batch.source_batch_id, 'config_snapshot': batch.config_snapshot}
    if detail:
        result['items'] = items
    return result


def cancel_batch(s, batch):
    batch.cancel_requested = True
    ids = [item['run_id'] for item in batch.manifest]
    for job in s.scalars(select(Job).where(Job.run_id.in_(ids)).with_for_update()):
        if job.status == 'queued':
            job.status = 'cancelled'
            if job.job_type == 'run':
                run = s.get(Run, job.run_id)
                run.status, run.finished_at = 'cancelled', now()
                for call in s.scalars(select(Call).where(Call.run_id == run.id, Call.status == 'queued')):
                    call.status, call.finished_at = 'cancelled', now()
        elif job.status == 'running' and job.job_type == 'run':
            s.get(Run, job.run_id).status = 'cancel_requested'
    s.flush()
