"""Durable side effects shared by graph nodes."""
import httpx
from sqlalchemy import select, func
from .config import settings
from .contracts import canonical
from .db import (transaction, Call, Run, ModelRequest, ToolExecution, Job, Snapshot, Evaluation, Response)
from .providers import respond, ProviderError
from .services import log


lease_check = lambda: None


class Cancelled(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class ToolUnknown(RuntimeError):
    pass


class ToolDenied(RuntimeError):
    pass


def check_cancel(call_id, phase='online'):
    lease_check()
    with transaction() as s:
        call = s.get(Call, call_id)
        run = s.get(Run, call.run_id)
        if phase == 'online' and run.status in ('cancel_requested', 'cancelled'):
            raise Cancelled('실행 취소 요청')


def model_operation(call_id, snapshot_id, operation_key, payload, context, evaluation_id=None, phase='online'):
    check_cancel(call_id, phase)
    with transaction() as s:
        snapshot = s.get(Snapshot, snapshot_id)
        if snapshot is None or snapshot.call_id != call_id:
            raise ValueError('Request snapshot belongs to another call')
        if evaluation_id:
            evaluation = s.get(Evaluation, evaluation_id)
            if evaluation is None or s.get(Response, evaluation.response_id).call_id != call_id:
                raise ValueError('Judge evaluation belongs to another call')
        rows = list(s.scalars(select(ModelRequest).where(ModelRequest.call_id == call_id,
                             ModelRequest.operation_key == operation_key).order_by(ModelRequest.attempt_number)))
        done = next((r for r in rows if r.status == 'completed'), None)
        if done:
            return done.response_payload
        config = s.get(Run, s.get(Call, call_id).run_id).config_snapshot
        start = len(rows)
        # Inflight after process death is an unknown transport outcome, never a success.
        for row in rows:
            if row.status == 'running':
                row.status = 'unknown'
                row.error = {'code': 'unknown_after_restart', 'duplicate_provider_call_possible': True}
    if len(canonical(payload).encode()) > config['budget']['max_input_bytes']:
        raise BudgetExceeded('context_limit: 입력을 삭제하거나 요약하지 않았습니다.')
    for attempt in range(start + 1, config['transport_attempts'] + 1):
        check_cancel(call_id, phase)
        with transaction() as s:
            call = s.get(Call, call_id)
            # The worker is globally serialized. Count reservations before issuing a request.
            query = select(func.count()).select_from(ModelRequest).join(Call, Call.id == ModelRequest.call_id).where(Call.run_id == call.run_id)
            if phase == 'offline':
                query = query.where(ModelRequest.operation_key.like('offline:%'))
            else:
                query = query.where(~ModelRequest.operation_key.like('offline:%'))
            count = s.scalar(query)
            if count >= config['budget']['max_requests']:
                raise BudgetExceeded('request_budget_exhausted')
            row = ModelRequest(call_id=call_id, input_snapshot_id=snapshot_id, evaluation_id=evaluation_id,
                               operation_key=operation_key, attempt_number=attempt,
                               request_payload=payload, status='running')
            s.add(row)
            s.flush()
            request_id = row.id
        log(call_id, 'model_request', 'started', {'attempt': attempt, 'kind': context['kind']}, operation_key=operation_key)
        try:
            raw = respond(payload, context)
        except ProviderError as exc:
            with transaction() as s:
                row = s.get(ModelRequest, request_id)
                row.status, row.error, row.response_payload = 'failed', {'code': str(exc)}, exc.raw
            log(call_id, 'model_request', 'retry' if exc.retryable and attempt < 2 else 'failed', {'error': str(exc)}, operation_key=operation_key)
            if not exc.retryable:
                raise
        else:
            lease_check()
            with transaction() as s:
                row = s.get(ModelRequest, request_id)
                row.status, row.response_payload, row.usage = 'completed', raw, raw.get('usage')
            log(call_id, 'model_request', 'completed', {'request_id': request_id}, operation_key=operation_key)
            return raw
    raise ProviderError('Transport attempts exhausted')


def execute_tool(call_id, arguments, response_version):
    check_cancel(call_id)
    if response_version:
        raise ToolDenied('복구 중 새 도구 호출 금지')
    with transaction() as s:
        call = s.get(Call, call_id)
        if call.role != 'test_runner':
            raise ToolDenied('이 역할은 도구를 사용할 수 없습니다.')
        tool = s.scalar(select(ToolExecution).where(ToolExecution.call_id == call_id, ToolExecution.operation_key == 'original-tests'))
        if tool and tool.status == 'completed':
            return tool.id
        existed = tool is not None
        if not tool:
            tool = ToolExecution(call_id=call_id, operation_key='original-tests', arguments=arguments, status='running')
            s.add(tool)
            s.flush()
        tool_id = tool.id
        mode = s.get(Run, call.run_id).config_snapshot['sandbox_mode']
    if mode == 'mock':
        result = {'status': 'not_executed', 'exit_code': None, 'tests': None, 'failures': None, 'errors': None,
                  'mock': True, 'reason': 'Mock sandbox: no code was executed.'}
        envelope = {'state': 'completed', 'result': result, 'stdout': '', 'stderr': '', 'junit_xml': ''}
    else:
        headers = {'Authorization': f'Bearer {settings.sandbox_token}'}
        try:
            with httpx.Client(timeout=45) as client:
                if existed:
                    response = client.get(f'{settings.sandbox_url}/executions/{tool_id}', headers=headers)
                    if response.status_code == 404:
                        # Executor journals every launch before starting a container.
                        response = client.post(f'{settings.sandbox_url}/executions/{tool_id}', json=arguments, headers=headers)
                else:
                    response = client.post(f'{settings.sandbox_url}/executions/{tool_id}', json=arguments, headers=headers)
                response.raise_for_status()
                envelope = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ToolUnknown(f'Sandbox 상태 확인 필요: {type(exc).__name__}') from exc
        if envelope.get('state') != 'completed':
            raise ToolUnknown('Sandbox 실행 상태가 아직 확정되지 않았습니다. 재개 시 상태를 조회합니다.')
    lease_check()
    with transaction() as s:
        tool = s.get(ToolExecution, tool_id)
        tool.status, tool.result = 'completed', envelope['result']
        tool.stdout, tool.stderr, tool.junit_xml = (envelope.get(k, '') for k in ('stdout', 'stderr', 'junit_xml'))
    return tool_id
