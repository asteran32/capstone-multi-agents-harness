import secrets
import yaml
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response as HTTPResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, func, text
from sqlalchemy.exc import IntegrityError

from .config import settings
from .contracts import RunCreate, BatchCreate, BatchRerun, BatchAnalysisUpdate, parse_policy, digest, ROLES, TERMINAL
from .db import (transaction, record, Batch, Run, Call, Task, Candidate, Job, Snapshot,
                 Response, Evaluation, ModelRequest, ToolExecution, Log, now)
from .services import create_run, Conflict, DEFAULT_POLICY
from .batches import create_batch, batch_summary, cancel_batch
from .exports import batch_results_csv
from .batch_analytics import batch_analytics, analysis_settings

app = FastAPI(title='Guardrail Lab', version='0.1.0', docs_url=None, redoc_url=None, openapi_url=None)
basic = HTTPBasic(auto_error=False)


def authenticate(credentials: HTTPBasicCredentials | None = Depends(basic)):
    if not settings.password:
        raise HTTPException(503, 'APP_PASSWORD를 설정해야 합니다.')
    if not credentials or not (secrets.compare_digest(credentials.username.encode(), settings.username.encode())
                              & secrets.compare_digest(credentials.password.encode(), settings.password.encode())):
        raise HTTPException(401, '인증이 필요합니다.')


@app.middleware('http')
async def headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


def require(s, model, id):
    item = s.get(model, str(id))
    if item is None:
        raise HTTPException(404, '기록을 찾을 수 없습니다.')
    return item


def key_required(key):
    if not key or len(key) > 200:
        raise HTTPException(422, 'Idempotency-Key 헤더가 필요합니다. (1~200자)')
    return key


def run_summary(s, run):
    calls = list(s.scalars(select(Call).where(Call.run_id == run.id)))
    model_type = 'mock' if all(run.config_snapshot[k] == 'mock' for k in ('agent_model', 'judge_model')) else 'live'
    return {**record(run), 'model_type': model_type, 'total_calls': len(calls),
            'finished_calls': sum(c.status in TERMINAL for c in calls),
            'failed_calls': sum(c.status in ('failed', 'blocked', 'unresolved') for c in calls),
            'jobs': [record(j) for j in s.scalars(select(Job).where(Job.run_id == run.id))]}


@app.get('/health/live')
def live():
    return {'status': 'ok'}


@app.get('/health/ready')
def ready():
    try:
        with transaction() as s:
            s.execute(text('SELECT 1 FROM app.runs LIMIT 1'))
        return {'status': 'ok'}
    except Exception:
        raise HTTPException(503, 'Database is not ready')


@app.get('/api/config', dependencies=[Depends(authenticate)])
def config():
    raw = DEFAULT_POLICY.read_text()
    return {'models': ['mock', *settings.live_models], 'policy_raw': raw,
            'policy': parse_policy(raw).model_dump(), 'sandbox_mode': settings.sandbox_mode}


class PolicyInput(BaseModel):
    raw: str = Field(max_length=100000)


@app.post('/api/policies/validate', dependencies=[Depends(authenticate)])
def validate_policy(body: PolicyInput):
    try:
        policy = parse_policy(body.raw).model_dump()
        return {'policy': policy, 'sha256': digest(policy)}
    except (ValueError, yaml.YAMLError) as exc:
        raise HTTPException(422, str(exc))


@app.get('/api/tasks', dependencies=[Depends(authenticate)])
def tasks():
    with transaction() as s:
        ordered = sorted(s.scalars(select(Task)), key=lambda t: int(t.source_task_id.rsplit('/', 1)[1]))
        return [{'id': t.id, 'source_task_id': t.source_task_id, 'prompt': t.source_record['prompt'],
                 'entry_point': t.source_record['entry_point']} for t in ordered]


@app.get('/api/tasks/{id}/candidates', dependencies=[Depends(authenticate)])
def candidates(id: UUID):
    with transaction() as s:
        require(s, Task, id)
        return [record(c) for c in s.scalars(select(Candidate).where(Candidate.task_id == str(id)).order_by(Candidate.private_label))]


@app.post('/api/runs', status_code=202, dependencies=[Depends(authenticate)])
def new_run(body: RunCreate, idempotency_key: str | None = Header(default=None)):
    key = key_required(idempotency_key)
    try:
        with transaction() as s:
            run = create_run(s, body, key)
            result = {'id': run.id, 'status': run.status}
        return result
    except IntegrityError:
        # A concurrent duplicate may have won the unique-key race.
        with transaction() as s:
            existing = s.scalar(select(Run).where(Run.idempotency_key == key))
            if existing and existing.creation_request_sha256 == digest(body.model_dump(mode='json')):
                return {'id': existing.id, 'status': existing.status}
        raise HTTPException(409, '중복 명령 또는 참조 충돌')
    except Conflict as exc:
        raise HTTPException(409, str(exc))
    except (ValueError, yaml.YAMLError) as exc:
        raise HTTPException(422, str(exc))


@app.get('/api/runs', dependencies=[Depends(authenticate)])
def runs(mode: str | None = None, status: str | None = None, since: datetime | None = None,
         until: datetime | None = None, model_type: Literal['mock', 'live'] | None = None,
         offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    with transaction() as s:
        q = select(Run)
        for clause in [Run.mode == mode if mode else None, Run.status == status if status else None,
                       Run.created_at >= since if since else None, Run.created_at < until if until else None]:
            if clause is not None:
                q = q.where(clause)
        if model_type:
            mock_models = ((Run.config_snapshot['agent_model'].as_string() == 'mock') &
                           (Run.config_snapshot['judge_model'].as_string() == 'mock'))
            q = q.where(mock_models if model_type == 'mock' else ~mock_models)
        total = s.scalar(select(func.count()).select_from(q.subquery()))
        items = [run_summary(s, r) for r in s.scalars(q.order_by(Run.created_at.desc(), Run.id).offset(offset).limit(limit))]
        return {'items': items, 'total': total}


def submit_batch(body, key, source_id=None):
    key = key_required(key)
    try:
        with transaction() as s:
            source = require(s, Batch, source_id) if source_id else None
            if source:
                body = BatchCreate(**{**source.config_snapshot, 'mode': body.mode})
            batch = create_batch(s, body, key, source)
            return batch_summary(s, batch, detail=True)
    except Conflict as exc:
        raise HTTPException(409, str(exc))
    except (ValueError, yaml.YAMLError) as exc:
        raise HTTPException(422, str(exc))
    except IntegrityError:
        request_hash = digest({'settings': body.model_dump(mode='json'),
                               'source_batch_id': str(source_id) if source_id else None})
        with transaction() as s:
            existing = s.scalar(select(Batch).where(Batch.idempotency_key == key))
            if existing and existing.creation_request_sha256 == request_hash:
                return batch_summary(s, existing, detail=True)
        raise HTTPException(409, '중복 일괄 실행 요청 또는 데이터 충돌')


@app.post('/api/batches', status_code=202, dependencies=[Depends(authenticate)])
def new_batch(body: BatchCreate, idempotency_key: str | None = Header(default=None)):
    return submit_batch(body, idempotency_key)


@app.get('/api/batches', dependencies=[Depends(authenticate)])
def batches(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    with transaction() as s:
        total = s.scalar(select(func.count()).select_from(Batch))
        rows = list(s.scalars(select(Batch).order_by(Batch.created_at.desc(), Batch.id).offset(offset).limit(limit)))
        return {'items': [batch_summary(s, b) for b in rows], 'total': total}


@app.get('/api/batches/{id}', dependencies=[Depends(authenticate)])
def batch_detail(id: UUID):
    with transaction() as s:
        return batch_summary(s, require(s, Batch, id), detail=True)


@app.get('/api/batches/{id}/results.csv', dependencies=[Depends(authenticate)])
def batch_results(id: UUID):
    with transaction() as s:
        if s.get_bind().dialect.name == 'postgresql':
            # Progress, evaluations and usage must describe the same database snapshot.
            s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        content, filename = batch_results_csv(s, require(s, Batch, id))
    return HTTPResponse(content, media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.get('/api/batches/{id}/analytics', dependencies=[Depends(authenticate)])
def batch_analysis(id: UUID):
    with transaction() as s:
        if s.get_bind().dialect.name == 'postgresql':
            s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        return batch_analytics(s, require(s, Batch, id))


@app.put('/api/batches/{id}/analysis-settings', dependencies=[Depends(authenticate)])
def update_batch_analysis(id: UUID, body: BatchAnalysisUpdate):
    with transaction() as s:
        batch = s.scalar(select(Batch).where(Batch.id == str(id)).with_for_update())
        if not batch:
            raise HTTPException(404, '기록을 찾을 수 없습니다.')
        current = analysis_settings(batch)
        if body.revision != current['revision']:
            raise HTTPException(409, '분석 설정이 변경되었습니다. 새로고침 후 다시 저장하세요.')
        if body.m4_threshold != current['m4_threshold']:
            change = {'m4_threshold': body.m4_threshold, 'revision': current['revision'] + 1,
                      'updated_at': now().isoformat()}
            batch.analysis_settings = {**change, 'history': [*current['history'],
                {key: current[key] for key in ('m4_threshold', 'revision', 'updated_at')}]}
        return analysis_settings(batch)


@app.post('/api/batches/{id}/rerun', status_code=202, dependencies=[Depends(authenticate)])
def rerun_batch(id: UUID, body: BatchRerun, idempotency_key: str | None = Header(default=None)):
    return submit_batch(body, idempotency_key, id)


@app.post('/api/batches/{id}/cancel', dependencies=[Depends(authenticate)])
def stop_batch(id: UUID):
    with transaction() as s:
        batch = s.scalar(select(Batch).where(Batch.id == str(id)).with_for_update())
        if not batch:
            raise HTTPException(404, '기록을 찾을 수 없습니다.')
        state = batch_summary(s, batch)
        if state['status'] in ('queued', 'running', 'cancel_requested'):
            cancel_batch(s, batch)
        return batch_summary(s, batch, detail=True)


@app.get('/api/runs/{id}', dependencies=[Depends(authenticate)])
def run_detail(id: UUID):
    with transaction() as s:
        return run_summary(s, require(s, Run, id))


@app.get('/api/runs/{id}/calls', dependencies=[Depends(authenticate)])
def calls(id: UUID, role: str | None = None, verdict: str | None = None,
          phase: str = 'online', offset: int = Query(0, ge=0), limit: int = Query(60, ge=1, le=100)):
    with transaction() as s:
        require(s, Run, id)
        q = select(Call).where(Call.run_id == str(id))
        if role:
            q = q.where(Call.role == role)
        items = []
        for call in s.scalars(q.order_by(Call.call_seq)):
            evs = list(s.scalars(select(Evaluation).join(Response, Response.id == Evaluation.response_id)
                                .where(Response.call_id == call.id, Evaluation.phase == phase).order_by(Response.response_version)))
            if verdict and (not evs or evs[0].aggregate_verdict != verdict):
                continue
            items.append({**record(call), 'evaluations': [record(e) for e in evs],
                          'response_count': s.scalar(select(func.count()).select_from(Response).where(Response.call_id == call.id))})
        return {'items': items[offset:offset + limit], 'total': len(items)}


@app.get('/api/calls/{id}', dependencies=[Depends(authenticate)])
def call_detail(id: UUID):
    with transaction() as s:
        call = require(s, Call, id)
        result = record(call)
        for key, model in [('snapshots', Snapshot), ('responses', Response), ('requests', ModelRequest), ('tools', ToolExecution)]:
            result[key] = [record(r) for r in s.scalars(select(model).where(model.call_id == call.id).order_by(model.created_at, model.id))]
        result['evaluations'] = [record(e) for e in s.scalars(select(Evaluation).join(Response, Response.id == Evaluation.response_id)
                                .where(Response.call_id == call.id).order_by(Response.response_version, Evaluation.phase))]
        return result


@app.get('/api/calls/{id}/logs', dependencies=[Depends(authenticate)])
def logs(id: UUID, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
    with transaction() as s:
        require(s, Call, id)
        rows = list(s.scalars(select(Log).where(Log.call_id == str(id), Log.event_seq > after).order_by(Log.event_seq).limit(limit)))
        return {'items': [record(r) for r in rows], 'next_after': rows[-1].event_seq if rows else after, 'has_more': len(rows) == limit}


@app.post('/api/runs/{id}/cancel', dependencies=[Depends(authenticate)])
def cancel(id: UUID):
    with transaction() as s:
        run = require(s, Run, id)
        if run.status not in TERMINAL:
            run.status = 'cancel_requested'
        return {'id': run.id, 'status': run.status}


@app.post('/api/runs/{id}/resume', status_code=202, dependencies=[Depends(authenticate)])
def resume(id: UUID):
    with transaction() as s:
        run = require(s, Run, id)
        batch_id = run.config_snapshot.get('batch_id')
        if batch_id and s.get(Batch, batch_id).cancel_requested:
            raise HTTPException(409, '취소된 일괄 실행입니다. 같은 구성으로 새 일괄 실행을 시작하세요.')
        job = s.scalar(select(Job).where(Job.run_id == run.id, Job.job_type == 'run').with_for_update())
        if run.status not in ('failed', 'cancelled'):
            raise HTTPException(409, '실패하거나 취소된 실행만 재개할 수 있습니다.')
        # Calls that completed with a model/tool error remain experimental observations.
        # Only interrupted calls and unstarted cancellations are resumable.
        pending = list(s.scalars(select(Call).where(Call.run_id == run.id,
                             Call.status.in_(['queued', 'running', 'cancelled']))))
        if not pending:
            raise HTTPException(409, '재개할 미완료 작업이 없습니다. 새 실행을 시작하세요.')
        for call in pending:
            call.status, call.finished_at = 'queued', None
        run.status, run.finished_at, run.error = 'queued', None, None
        job.status, job.lease_owner, job.lease_expires_at, job.error = 'queued', None, None, None
        return {'id': run.id, 'status': run.status}


@app.post('/api/runs/{id}/evaluate', status_code=202, dependencies=[Depends(authenticate)])
def evaluate(id: UUID, idempotency_key: str | None = Header(default=None)):
    key_required(idempotency_key)
    with transaction() as s:
        run = s.scalar(select(Run).where(Run.id == str(id)).with_for_update())
        if not run:
            raise HTTPException(404, '기록을 찾을 수 없습니다.')
        if run.mode not in ('Base', 'L1') or run.status not in TERMINAL:
            raise HTTPException(409, '종료된 Base/L1 실행의 저장 응답만 평가할 수 있습니다.')
        version = run.config_snapshot['evaluation_version']
        job_key = f'offline:{run.id}:{version}'
        request_hash = digest({'run_id': run.id, 'evaluation_version': version})
        # Serialize on the run; the immutable version defines the only allowed payload.
        job = s.scalar(select(Job).where(Job.job_key == job_key))
        if not job:
            job = Job(run_id=run.id, job_key=job_key, job_type='offline',
                      payload={'request_hash': request_hash, 'idempotency_key': idempotency_key, 'evaluation_version': version})
            s.add(job)
            s.flush()
        elif job.payload['request_hash'] != request_hash:
            raise HTTPException(409, '평가 요청 충돌')
        elif job.status == 'failed':
            job.status, job.error, job.lease_owner = 'queued', None, None
        return {'id': job.id, 'status': job.status}


if settings.frontend.exists():
    app.mount('/assets', StaticFiles(directory=settings.frontend / 'assets'), name='assets')

    @app.get('/')
    def index():
        return FileResponse(settings.frontend / 'index.html')
