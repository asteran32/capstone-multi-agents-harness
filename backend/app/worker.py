"""One process consumes PostgreSQL jobs. Run with python -m app.worker."""
import logging
import os
import socket
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, or_, text
from sqlalchemy.orm import aliased
from .db import transaction, engine, Batch, Job, Run, Call, Response, Snapshot, Evaluation, now
from .contracts import TERMINAL
from .graphs import agent_graph, offline_graph
from .runtime import Cancelled, ToolDenied, ToolUnknown, BudgetExceeded
from .providers import ProviderError
from .services import log
from . import runtime

logger = logging.getLogger(__name__)
OWNER = f'{socket.gethostname()}:{os.getpid()}:{uuid4()}'


def claim():
    with transaction() as s:
        dependency = aliased(Job)
        dependency_key = Job.payload['depends_on_key'].as_string()
        ready = or_(dependency_key.is_(None), select(dependency.id).where(
            dependency.job_key == dependency_key,
            dependency.status.in_(['completed', 'failed', 'cancelled'])).exists())
        job = s.scalar(select(Job).where(or_(Job.status == 'queued',
                       (Job.status == 'running') & (Job.lease_expires_at < now())))
                       .where(ready).order_by(Job.created_at, Job.id).with_for_update(skip_locked=True).limit(1))
        if not job:
            return None
        job.status, job.lease_owner = 'running', OWNER
        job.heartbeat_at, job.lease_expires_at = now(), now() + timedelta(seconds=60)
        return job.id


@contextmanager
def heartbeat(job_id):
    stop = threading.Event()
    lost = threading.Event()

    def beat():
        while not stop.wait(10):
            try:
                with transaction() as s:
                    job = s.get(Job, job_id)
                    if job.lease_owner != OWNER or job.status != 'running':
                        lost.set()
                        return
                    job.heartbeat_at, job.lease_expires_at = now(), now() + timedelta(seconds=60)
            except Exception:
                lost.set()
                return

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield lost
    finally:
        stop.set()
        thread.join(timeout=2)


def invoke_or_resume(graph, state, thread_id):
    config = {'configurable': {'thread_id': thread_id}, 'recursion_limit': 80}
    checkpoint = graph.get_state(config)
    if checkpoint.values and not checkpoint.next:
        return checkpoint.values
    return graph.invoke(None if checkpoint.values else state, config, durability='sync')


def process_job(job_id, agent, offline):
    with transaction() as s:
        job = s.get(Job, job_id)
        run = s.get(Run, job.run_id)
        run_id, job_type, batch_id = run.id, job.job_type, job.payload.get('batch_id')
        if job_type == 'run' and run.status != 'cancel_requested':
            run.status, run.error = 'running', None
        call_ids = list(s.scalars(select(Call.id).where(Call.run_id == run.id).order_by(Call.call_seq)))
    with heartbeat(job_id) as lost:
        def verify_lease():
            if lost.is_set():
                raise RuntimeError('Worker lease lost')
            if batch_id:
                with transaction() as s:
                    if s.get(Batch, batch_id).cancel_requested:
                        raise Cancelled('일괄 실행 취소 요청')
        runtime.lease_check = verify_lease
        try:
            verify_lease()
            if job_type == 'run':
                for call_id in call_ids:
                    verify_lease()
                    with transaction() as s:
                        call, run = s.get(Call, call_id), s.get(Run, run_id)
                        if run.status == 'cancel_requested':
                            raise Cancelled('취소 요청')
                        if call.status in TERMINAL:
                            continue
                    try:
                        invoke_or_resume(agent, {'call_id': call_id}, call_id)
                    except Cancelled:
                        with transaction() as s:
                            call = s.get(Call, call_id)
                            call.status, call.finished_at = 'cancelled', now()
                            call.error = {'code': 'cancelled'}
                        raise
                    except ToolUnknown:
                        # Keep this call unfinished; resume must inspect the executor, not rerun tests.
                        raise
                    except (ProviderError, BudgetExceeded, ToolDenied, ValueError) as exc:
                        with transaction() as s:
                            call = s.get(Call, call_id)
                            call.status = 'blocked' if isinstance(exc, ToolDenied) else 'failed'
                            call.finished_at, call.error = now(), {'code': type(exc).__name__, 'message': str(exc)}
                        log(call_id, 'call', 'failed', {'error': str(exc)})
            elif job_type == 'reevaluation':
                from .reevaluation_worker import process_reevaluation
                with transaction() as s:
                    reevaluation_id = s.get(Job, job_id).payload['reevaluation_id']
                process_reevaluation(reevaluation_id)
            elif job_type == 'offline':
                with transaction() as s:
                    items = list(s.scalars(select(Response).join(Call, Call.id == Response.call_id)
                                 .where(Call.run_id == run_id).order_by(Call.call_seq, Response.response_version)))
                    ids = [item.id for item in items]
                for response_id in ids:
                    verify_lease()
                    with transaction() as s:
                        response = s.get(Response, response_id)
                        call = s.get(Call, response.call_id)
                        run = s.get(Run, run_id)
                        version = run.config_snapshot['evaluation_version']
                        ev = s.scalar(select(Evaluation).where(Evaluation.response_id == response.id,
                                      Evaluation.phase == 'offline', Evaluation.evaluation_version == version))
                        if ev and ev.status in ('completed', 'error'):
                            continue
                        snapshot = s.scalar(select(Snapshot).where(Snapshot.call_id == call.id,
                                                                   Snapshot.response_version == response.response_version))
                        state = {'call_id': call.id, 'run_id': run.id, 'role': call.role, 'mode': run.mode,
                                 'response_id': response.id, 'response_version': response.response_version,
                                 'input_snapshot_id': snapshot.id, 'phase': 'offline', 'judge_attempt': 0}
                    invoke_or_resume(offline, state, f'offline:{response_id}:{version}')
            else:
                raise ValueError(f'Unknown job type: {job_type}')
            verify_lease()
            with transaction() as s:
                job = s.get(Job, job_id)
                if job.lease_owner != OWNER or lost.is_set():
                    raise RuntimeError('Worker lease lost before commit')
                job.status, job.lease_expires_at = 'completed', None
                if job_type == 'run':
                    run = s.get(Run, run_id)
                    if run.status == 'cancel_requested':
                        raise Cancelled('취소 요청')
                    statuses = list(s.scalars(select(Call.status).where(Call.run_id == run_id)))
                    run.status = 'failed' if any(x in ('failed', 'blocked') for x in statuses) else 'completed'
                    run.finished_at = now()
        except Cancelled:
            with transaction() as s:
                run, job = s.get(Run, run_id), s.get(Job, job_id)
                job.status, job.lease_expires_at = 'cancelled', None
                if job_type == 'run':
                    run.status, run.finished_at = 'cancelled', now()
                    for call in s.scalars(select(Call).where(Call.run_id == run_id,
                                                           Call.status.in_(['queued', 'running']))):
                        call.status, call.finished_at = 'cancelled', now()
        except Exception as exc:
            with transaction() as s:
                job = s.get(Job, job_id)
                if job.lease_owner == OWNER:
                    job.status, job.error = 'failed', {'code': type(exc).__name__, 'message': str(exc)}
                    if job_type == 'run':
                        run = s.get(Run, run_id)
                        run.status, run.error = 'failed', job.error
            logger.exception('Job failed: %s', job_id)
        finally:
            runtime.lease_check = lambda: None


@contextmanager
def postgres_checkpointer():
    import psycopg
    from psycopg.rows import dict_row
    from langgraph.checkpoint.postgres import PostgresSaver
    from .config import settings
    dsn = settings.database_url.replace('postgresql+psycopg://', 'postgresql://')
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row, options='-c search_path=checkpoint') as connection:
        saver = PostgresSaver(connection)
        saver.setup()
        yield saver


def main():
    logging.basicConfig(level=logging.INFO)
    # A live advisory lock prevents overlapping workers, including stale lease claimers.
    with engine.connect() as lock:
        if not lock.scalar(text('SELECT pg_try_advisory_lock(728341)')):
            raise SystemExit('MVP worker is already running')
        with postgres_checkpointer() as saver:
            agent, offline = agent_graph(saver), offline_graph(saver)
            while True:
                lock.execute(text('SELECT 1'))
                job_id = claim()
                if job_id:
                    process_job(job_id, agent, offline)
                else:
                    time.sleep(1)


if __name__ == '__main__':
    main()
