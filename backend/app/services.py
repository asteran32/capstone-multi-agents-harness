from pathlib import Path
from sqlalchemy import select, func
from .contracts import (ROLES, MODES, TERMINAL, digest, parse_policy, render_policy,
                        evaluation_version)
from .config import ROOT, settings
from .evaluation_v2 import evaluation_profile
from .db import (Run, Call, Task, Candidate, Snapshot, Response, ToolExecution,
                 Job, Log, transaction, now, uid)

DEFAULT_POLICY = ROOT / 'policies/default.yaml'


class Conflict(ValueError):
    pass


def create_run(s, body, key):
    request = body.model_dump(mode='json')
    request_hash = digest(request)
    existing = s.scalar(select(Run).where(Run.idempotency_key == key))
    if existing:
        if existing.creation_request_sha256 != request_hash:
            raise Conflict('같은 idempotency key에 다른 요청이 있습니다.')
        return existing
    task, candidate = s.get(Task, str(body.task_id)), s.get(Candidate, str(body.candidate_id))
    if not task or not candidate or candidate.task_id != task.id:
        raise ValueError('문제와 후보가 일치하지 않습니다.')
    for model in (body.agent_model, body.judge_model):
        if model not in ('mock', *settings.live_models):
            raise ValueError('서버에 등록되지 않은 모델입니다.')
    raw = body.policy_raw if body.policy_raw is not None else DEFAULT_POLICY.read_text()
    policy = parse_policy(raw).model_dump()
    profile = evaluation_profile()
    config = {**request, 'policy_raw': raw, 'policy': policy, 'policy_hash': digest(policy),
              'renderer_version': '1', 'data_version': task.source_version,
              'source_task_id': task.source_task_id, 'entry_point': task.source_record['entry_point'],
              'task_sha256': task.source_sha256, 'candidate_sha256': candidate.code_sha256,
              'transport_attempts': 2, 'judge_attempts': 2, 'schema_version': 1,
              'sandbox_mode': settings.sandbox_mode,
              'recovery_strategy': 'feedback', 'evaluation_profile': profile,
              'evaluation_version': evaluation_version(policy, body.judge_model, body.budget.max_output_tokens, profile)}
    run = Run(id=uid(), idempotency_key=key, creation_request_sha256=request_hash,
              mode=body.mode, config_snapshot=config)
    s.add(run)
    s.flush()
    plan = []
    for round_index in range(1, body.rounds + 1):
        for role in ROLES:
            call = Call(id=uid(), run_id=run.id, task_id=task.id,
                        candidate_id=None if role == ROLES[0] else candidate.id,
                        role=role, round_index=round_index, call_seq=len(plan) + 1,
                        user_prompt=body.prompts_by_role[role])
            s.add(call)
            plan.append({'call_id': call.id, 'role': role, 'round': round_index, 'call_seq': call.call_seq})
    run.config_snapshot = {**config, 'execution_plan': plan}
    s.add(Job(run_id=run.id, job_key=f'run:{run.id}', job_type='run', payload={'request_hash': request_hash}))
    return run


def task_input(s, call):
    task = s.get(Task, call.task_id)
    raw = task.source_record
    result = {'prompt': raw['prompt']}
    if call.role == 'problem_designer':
        result.update(entry_point=raw['entry_point'], canonical_solution=raw['canonical_solution'])
    else:
        candidate = s.get(Candidate, call.candidate_id)
        expected = s.get(Run, call.run_id).config_snapshot['candidate_sha256']
        if candidate.task_id != task.id or digest(candidate.code) != expected:
            raise ValueError('고정 후보 코드 해시 불일치')
        result.update(candidate_code=candidate.code, candidate_sha256=candidate.code_sha256)
        if call.role == 'test_runner':
            result.update(test=raw['test'], entry_point=raw['entry_point'])
    return result


def select_history(s, call):
    calls = list(s.scalars(select(Call).where(
        Call.run_id == call.run_id, Call.role == call.role, Call.call_seq < call.call_seq,
        Call.status.in_(TERMINAL), Call.started_at.is_not(None)
    ).order_by(Call.call_seq.desc()).limit(5)))
    history = []
    for previous in reversed(calls):
        snap = s.scalar(select(Snapshot).where(Snapshot.call_id == previous.id, Snapshot.response_version == 0))
        responses = list(s.scalars(select(Response).where(Response.call_id == previous.id).order_by(Response.response_version)))
        item = {'call_id': previous.id, 'user_prompt': previous.user_prompt,
                'task_input': snap.task_input if snap else task_input(s, previous),
                'responses': [{'response_version': r.response_version, 'raw_text': r.raw_text} for r in responses] or None,
                'status': previous.status, 'error': previous.error}
        if previous.role == 'test_runner':
            tool = s.scalar(select(ToolExecution).where(ToolExecution.call_id == previous.id))
            item['tool_result'] = tool.result if tool else None
        history.append(item)
    return history


def ensure_snapshot(s, call, version, feedback=None):
    existing = s.scalar(select(Snapshot).where(Snapshot.call_id == call.id, Snapshot.response_version == version))
    if existing:
        return existing
    run = s.get(Run, call.run_id)
    config = run.config_snapshot
    policy = next(p for p in config['policy']['roles'] if p['role'] == call.role)
    parent = s.scalar(select(Snapshot).where(Snapshot.call_id == call.id, Snapshot.response_version == 0)) if version else None
    if version and not parent:
        raise ValueError('복구 원본 snapshot이 없습니다.')
    history = [] if version else select_history(s, call)
    dto = parent.task_input if parent else task_input(s, call)
    prompt = parent.role_prompt if parent else render_policy(policy, MODES[run.mode][0])
    if version and config.get('recovery_strategy') == 'feedback' and not feedback:
        raise ValueError('Feedback recovery requires immutable violation feedback')
    content = {'role_prompt': prompt, 'task_input': dto, 'ordered_history': history}
    if feedback:
        content['recovery_feedback'] = feedback
    snapshot = Snapshot(call_id=call.id, response_version=version, role_prompt=prompt,
                        task_input=dto, ordered_history=history, parent_snapshot_id=parent.id if parent else None,
                        reset_reason=('confirmed_violation_feedback' if feedback else 'confirmed_violation') if parent else None,
                        recovery_feedback=feedback, content_sha256=digest(content))
    s.add(snapshot)
    s.flush()
    return snapshot


def log(call_id, node, status, details=None, layer=None, operation_key=None):
    with transaction() as s:
        # Serializes event numbering even when a heartbeat/error path logs concurrently.
        s.scalar(select(Call).where(Call.id == call_id).with_for_update())
        seq = s.scalar(select(func.coalesce(func.max(Log.event_seq), 0)).where(Log.call_id == call_id)) + 1
        s.add(Log(call_id=call_id, event_seq=seq, node_name=node, layer=layer,
                  operation_key=operation_key, status=status, details=details or {}))
