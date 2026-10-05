import json
from functools import wraps
from typing import TypedDict
from sqlalchemy import select
from langgraph.graph import StateGraph, START, END

from .contracts import (MODES, RUBRIC, JUDGE_JSON_INSTRUCTION, JudgeResult, RunnerReport, canonical, digest)
from .db import (transaction, Call, Run, Snapshot, Response, Evaluation, ToolExecution, now)
from .evaluation import lexical, check_judge, uncertain, execution_evidence, aggregate, metrics
from .evaluation_v2 import is_v2, runner_checks, resolve_judgment, recovery_feedback, judge_payload
from .providers import agent_payload, normalize, ProviderError
from .runtime import model_operation, execute_tool, check_cancel, BudgetExceeded, ToolDenied
from .services import ensure_snapshot, log


class State(TypedDict, total=False):
    run_id: str
    call_id: str
    role: str
    mode: str
    policy_version: str
    input_snapshot_id: str
    history_snapshot_id: str
    response_id: str
    response_version: int
    recovery_count: int
    judge_attempt: int
    tool_execution_id: str | None
    status: str
    phase: str
    step: int
    agent_result: dict
    evaluation_id: str
    judge_raw: str
    judge_error: str | None
    judge_error_kind: str | None
    judge_valid: bool
    verdict: str


def node(name, layer=None):
    def decorate(fn):
        @wraps(fn)
        def wrapped(state):
            call_id = state['call_id']
            check_cancel(call_id, state.get('phase', 'online'))
            log(call_id, name, 'started', layer=layer)
            try:
                result = fn(state)
            except Exception as exc:
                log(call_id, name, 'failed', {'error': str(exc), 'type': type(exc).__name__}, layer=layer)
                raise
            log(call_id, name, 'completed', layer=layer)
            return result
        return wrapped
    return decorate


@node('load_snapshot')
def load_snapshot(state):
    with transaction() as s:
        call = s.get(Call, state['call_id'])
        run = s.get(Run, call.run_id)
        call.status = 'running'
        call.started_at = call.started_at or now()
        snapshot = ensure_snapshot(s, call, 0)
        return {'run_id': run.id, 'role': call.role, 'mode': run.mode,
                'policy_version': run.config_snapshot['policy']['version'],
                'input_snapshot_id': snapshot.id, 'history_snapshot_id': snapshot.id,
                'response_version': 0, 'recovery_count': 0, 'judge_attempt': 0,
                'step': 0, 'phase': 'online', 'status': 'running'}


@node('apply_prompt', 'L1')
def apply_prompt(state):
    if not MODES[state['mode']][0]:
        log(state['call_id'], 'L1', 'skipped', {'reason': 'Base mode'}, layer='L1')
    return {}


@node('agent')
def agent(state):
    with transaction() as s:
        call = s.get(Call, state['call_id'])
        snapshot = s.get(Snapshot, state['input_snapshot_id'])
        config = s.get(Run, call.run_id).config_snapshot
        tool = s.get(ToolExecution, state['tool_execution_id']) if state.get('tool_execution_id') else None
        payload, context = agent_payload(snapshot, call, config, state['response_version'],
                                          tool.result if tool else None,
                                          state.get('agent_result', {}).get('output_items') if state['step'] else None)
    raw = model_operation(state['call_id'], snapshot.id,
                          f"online:agent:v{state['response_version']}:step{state['step']}", payload, context)
    return {'agent_result': normalize(raw)}


def after_agent(state):
    if not state['agent_result']['tool_calls']:
        return 'persist_response'
    return 'dispatch_tool'


@node('dispatch_tool')
def dispatch_tool(state):
    calls = state['agent_result']['tool_calls']
    if state['role'] != 'test_runner' or state['response_version'] or state['step'] >= 1 or len(calls) != 1:
        raise ToolDenied('권한 없는 도구 호출 또는 도구 반복 한도 초과')
    item = calls[0]
    try:
        args = json.loads(item['arguments'])
    except (KeyError, ValueError):
        raise ToolDenied('도구 인자 형식 오류')
    if item.get('name') != 'run_original_tests' or args != {}:
        raise ToolDenied('고정 원본 테스트 호출만 허용합니다.')
    with transaction() as s:
        snapshot = s.get(Snapshot, state['input_snapshot_id'])
        data = snapshot.task_input
        arguments = {'code': data['candidate_code'], 'test': data['test'], 'entry_point': data['entry_point'],
                     'code_sha256': data['candidate_sha256'], 'test_sha256': digest(data['test'])}
    tool_id = execute_tool(state['call_id'], arguments, state['response_version'])
    return {'tool_execution_id': tool_id, 'step': 1}


@node('persist_response')
def persist_response(state):
    with transaction() as s:
        row = s.scalar(select(Response).where(Response.call_id == state['call_id'], Response.response_version == state['response_version']))
        if not row:
            text = state['agent_result']['text']
            parsed, error = None, None
            if state['role'] == 'test_runner':
                try:
                    parsed = RunnerReport.model_validate_json(text).model_dump()
                except ValueError as exc:
                    error = str(exc)
            row = Response(call_id=state['call_id'], response_version=state['response_version'],
                           raw_text=text, parsed_output=parsed, format_error=error, response_sha256=digest(text))
            s.add(row)
            s.flush()
        return {'response_id': row.id, 'judge_attempt': 0, 'judge_error': None}


@node('disabled_evaluation', 'L2')
def disabled(state):
    with transaction() as s:
        config = s.get(Run, state['run_id']).config_snapshot
        existing = s.scalar(select(Evaluation).where(Evaluation.response_id == state['response_id'], Evaluation.phase == 'online',
                                                    Evaluation.evaluation_version == config['evaluation_version']))
        if not existing:
            s.add(Evaluation(response_id=state['response_id'], phase='online', evaluation_version=config['evaluation_version'],
                             status='disabled', aggregate_verdict=None))
    log(state['call_id'], 'L2', 'skipped', {'reason': 'mode disabled'}, layer='L2')
    return {'verdict': 'disabled'}


@node('lexical', 'L2')
def lexical_node(state):
    with transaction() as s:
        response = s.get(Response, state['response_id'])
        config = s.get(Run, s.get(Call, response.call_id).run_id).config_snapshot
        ev = s.scalar(select(Evaluation).where(Evaluation.response_id == response.id,
                      Evaluation.phase == state['phase'], Evaluation.evaluation_version == config['evaluation_version']))
        if not ev:
            ev = Evaluation(response_id=response.id, phase=state['phase'], evaluation_version=config['evaluation_version'])
            s.add(ev)
            s.flush()
        if ev.lexical_result is None:
            ev.lexical_result = lexical(response.raw_text, config['policy']['forbidden_terms'])
        return {'evaluation_id': ev.id}


@node('judge', 'L2')
def judge(state):
    with transaction() as s:
        call, snapshot, response = s.get(Call, state['call_id']), s.get(Snapshot, state['input_snapshot_id']), s.get(Response, state['response_id'])
        config = s.get(Run, call.run_id).config_snapshot
        tool = s.scalar(select(ToolExecution).where(ToolExecution.call_id == call.id))
        data = {'role': call.role, 'policy': next(p for p in config['policy']['roles'] if p['role'] == call.role),
                'task_input': snapshot.task_input, 'user_prompt': call.user_prompt,
                'current_response': response.raw_text, 'tool_result': tool.result if tool else None}
        # JSON mode validates input messages, so instructions alone are insufficient.
        payload = {'model': config['judge_model'], 'store': False, 'instructions': RUBRIC,
                   'input': [{'role': 'developer', 'content': JUDGE_JSON_INSTRUCTION},
                             {'role': 'user', 'content': canonical(data)}],
                   'max_output_tokens': config['budget']['max_output_tokens'],
                   'text': {'format': {'type': 'json_object'}}}
        if is_v2(config):
            ev = s.get(Evaluation, state['evaluation_id'])
            if ev.execution_evidence is None:
                ev.execution_evidence = runner_checks(call.role, response.raw_text, snapshot.task_input,
                    {'status': tool.status, 'arguments': tool.arguments, 'result': tool.result} if tool else None)
            payload, data = judge_payload(config, call.role, response.id, response.response_sha256,
                response.raw_text, snapshot.task_input, call.user_prompt, ev.execution_evidence,
                (ev.evidence_validation or {}) if state['judge_attempt'] else None)
    try:
        raw = model_operation(call.id, snapshot.id,
                              f"{state['phase']}:judge:{state['evaluation_id']}:validation{state['judge_attempt']}",
                              payload, {'kind': 'judge', 'data': data}, state['evaluation_id'], state['phase'])
        if raw.get('status') != 'completed':
            raise ProviderError('Incomplete Judge response', raw)
        # A completed but empty/non-JSON answer is a schema failure, eligible for the validation retry.
        text = ''.join(part.get('text', '') for item in raw.get('output', []) if item.get('type') == 'message'
                       for part in item.get('content', []) if part.get('type') == 'output_text')
        return {'judge_raw': text, 'judge_error': None, 'judge_error_kind': None}
    except (ProviderError, BudgetExceeded) as exc:
        return {'judge_raw': '', 'judge_error': str(exc),
                'judge_error_kind': 'budget' if isinstance(exc, BudgetExceeded) else 'provider'}


@node('validate_evidence', 'L2')
def validate(state):
    with transaction() as s:
        response, ev = s.get(Response, state['response_id']), s.get(Evaluation, state['evaluation_id'])
        error = state.get('judge_error')
        result = None
        config = s.get(Run, s.get(Call, state['call_id']).run_id).config_snapshot
        if error:
            validation = {'schema_version': 2, 'valid': False, 'schema_valid': None,
                          'evidence_required': None, 'evidence_valid': None, 'failed_rules': [],
                          'failure_kind': state.get('judge_error_kind') or 'unknown', 'error': error}
        else:
            result, validation = check_judge(state['judge_raw'], response.raw_text, state['role'], is_v2(config))
        if is_v2(config):
            validation = {**validation, 'schema_version': 3, 'judge_output': state.get('judge_raw', '')}
        valid, error = validation['valid'], validation['error']
        validation['attempt'] = state['judge_attempt'] + 1
        previous = ev.evidence_validation or {}
        attempts = {item['attempt']: item for item in previous.get('attempts', [])}
        if previous.get('attempt') and not attempts:
            attempts[previous['attempt']] = previous
        attempts[validation['attempt']] = validation
        ev.evidence_validation = {**validation, 'attempts': [attempts[key] for key in sorted(attempts)]}
        ev.judge_result = result if valid else uncertain(error or 'Judge evaluation failed')
    log(state['call_id'], 'judge_validation', 'completed' if valid else 'failed',
        {**validation, 'evaluation_id': state['evaluation_id'], 'phase': state['phase'],
         'response_version': state['response_version']}, layer='L2')
    return {'judge_valid': valid, 'judge_attempt': state['judge_attempt'] + 1}


def after_validation(state):
    # Retry invalid schema/evidence once; operational failures have their own bounded transport retries.
    return 'judge' if not state['judge_valid'] and state['judge_attempt'] < 2 and not state.get('judge_error') else 'aggregate'


@node('aggregate', 'L2')
def aggregate_node(state):
    with transaction() as s:
        ev, response, call = s.get(Evaluation, state['evaluation_id']), s.get(Response, state['response_id']), s.get(Call, state['call_id'])
        if ev.status in ('completed', 'error') and ev.aggregate_verdict:
            return {'verdict': ev.aggregate_verdict}
        tool = s.scalar(select(ToolExecution).where(ToolExecution.call_id == call.id))
        config = s.get(Run, call.run_id).config_snapshot
        evidence = execution_evidence(call.role, response.raw_text, tool.result if tool else None)
        if is_v2(config):
            snapshot = s.get(Snapshot, state['input_snapshot_id'])
            evidence = ev.execution_evidence or runner_checks(call.role, response.raw_text, snapshot.task_input,
                {'status': tool.status, 'arguments': tool.arguments, 'result': tool.result} if tool else None)
        ev.execution_evidence = evidence
        if is_v2(config):
            ev.judgment_resolution = resolve_judgment(ev.lexical_result, ev.judge_result, evidence, state['judge_valid'])
            ev.aggregate_verdict = ev.judgment_resolution['aggregate_verdict']
        else:
            ev.aggregate_verdict = aggregate(ev.lexical_result, ev.judge_result, evidence)
        ev.status = 'completed' if state['judge_valid'] else 'error'
        s.flush()
        if response.response_version == 0:
            rows = list(s.scalars(select(Evaluation).join(Response, Response.id == Evaluation.response_id)
                        .join(Call, Call.id == Response.call_id).where(
                            Call.run_id == call.run_id, Call.role == call.role, Call.call_seq <= call.call_seq,
                            Response.response_version == 0, Evaluation.phase == ev.phase,
                            Evaluation.evaluation_version == ev.evaluation_version,
                            Evaluation.aggregate_verdict.in_(['compliant', 'violated', 'uncertain'])
                        ).order_by(Call.call_seq.desc()).limit(5)))
            ev.metric_snapshot = metrics([{'id': r.id, 'verdict': r.aggregate_verdict} for r in reversed(rows)])
        return {'verdict': ev.aggregate_verdict}


def after_aggregate(state):
    return 'reset_context' if state['mode'] == 'FULL' and state['verdict'] == 'violated' and state['response_version'] == 0 else 'finish'


@node('reset_context', 'L3')
def reset_context(state):
    with transaction() as s:
        call = s.get(Call, state['call_id'])
        config = s.get(Run, call.run_id).config_snapshot
        feedback = recovery_feedback(s.get(Response, state['response_id']), s.get(Evaluation, state['evaluation_id'])) \
            if config.get('recovery_strategy') == 'feedback' else None
        snapshot = ensure_snapshot(s, call, 1, feedback)
        tool = s.scalar(select(ToolExecution).where(ToolExecution.call_id == state['call_id'], ToolExecution.status == 'completed'))
        return {'input_snapshot_id': snapshot.id, 'history_snapshot_id': snapshot.id,
                'response_version': 1, 'recovery_count': 1, 'judge_attempt': 0,
                'judge_error': None, 'judge_valid': False, 'step': 0, 'agent_result': {},
                'tool_execution_id': tool.id if tool else None}


@node('finish')
def finish(state):
    if not state.get('recovery_count'):
        log(state['call_id'], 'L3', 'skipped',
            {'reason': 'mode_disabled' if state['mode'] != 'FULL' else 'no_confirmed_violation'}, layer='L3')
    with transaction() as s:
        call = s.get(Call, state['call_id'])
        if state['response_version'] == 1 and state['verdict'] != 'compliant':
            call.status, call.selected_response_id = 'unresolved', None
            call.error = {'code': 'recovery_unresolved', 'message': '역할 위반을 해결하지 못했습니다. 원본과 평가 기록을 확인하세요.'}
        else:
            response = s.get(Response, state['response_id'])
            if response.call_id != call.id:
                raise ValueError('응답과 call 불일치')
            call.status, call.selected_response_id, call.error = 'completed', response.id, None
        call.finished_at = now()
        return {'status': call.status}


def agent_graph(checkpointer):
    graph = StateGraph(State)
    nodes = {'load_snapshot': load_snapshot, 'apply_prompt': apply_prompt, 'agent': agent,
             'dispatch_tool': dispatch_tool, 'persist_response': persist_response,
             'disabled': disabled, 'lexical': lexical_node, 'judge': judge, 'validate': validate,
             'aggregate': aggregate_node, 'reset_context': reset_context, 'finish': finish}
    for name, fn in nodes.items():
        graph.add_node(name, fn)
    graph.add_edge(START, 'load_snapshot')
    graph.add_edge('load_snapshot', 'apply_prompt')
    graph.add_edge('apply_prompt', 'agent')
    graph.add_conditional_edges('agent', after_agent, ['persist_response', 'dispatch_tool'])
    graph.add_edge('dispatch_tool', 'agent')
    graph.add_conditional_edges('persist_response', lambda s: 'lexical' if MODES[s['mode']][1] else 'disabled', ['lexical', 'disabled'])
    graph.add_edge('disabled', 'finish')
    graph.add_edge('lexical', 'judge')
    graph.add_edge('judge', 'validate')
    graph.add_conditional_edges('validate', after_validation, ['judge', 'aggregate'])
    graph.add_conditional_edges('aggregate', after_aggregate, ['reset_context', 'finish'])
    graph.add_edge('reset_context', 'agent')
    graph.add_edge('finish', END)
    return graph.compile(checkpointer=checkpointer)


def offline_graph(checkpointer):
    graph = StateGraph(State)
    for name, fn in [('lexical', lexical_node), ('judge', judge), ('validate', validate), ('aggregate', aggregate_node)]:
        graph.add_node(name, fn)
    graph.add_edge(START, 'lexical')
    graph.add_edge('lexical', 'judge')
    graph.add_edge('judge', 'validate')
    graph.add_conditional_edges('validate', after_validation, ['judge', 'aggregate'])
    graph.add_edge('aggregate', END)
    return graph.compile(checkpointer=checkpointer)
