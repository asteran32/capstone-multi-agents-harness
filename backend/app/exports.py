"""Analysis CSV: one row per scheduled role call, including unfinished calls."""
from collections import defaultdict
import csv
from datetime import datetime, timezone
import io
import json

from sqlalchemy import select

from .batches import batch_summary
from .db import Call, Evaluation, Job, ModelRequest, Response, Run, Snapshot, ToolExecution, now
from .evaluation_v2 import recovery_effect, uncertainty_reasons


EVALUATION_FIELDS = ('evaluation_status', 'verdict', 'lexical_verdict', 'lexical_match_count',
                     'info_verdict', 'scope_verdict', 'exec_verdict', 'judge_result',
                     'evidence_validation', 'execution_evidence', 'schema_valid', 'evidence_required',
                     'evidence_valid', 'validation_failure_kind', 'judgment_resolution', 'uncertainty_reasons')
METRIC_FIELDS = ('window_count', 'V', 'C', 'U', 'role_violation_rate', 'decision_coverage', 'warmup')
USAGE_FIELDS = ('request_count', 'failed_requests', 'usage_reported_requests',
                'input_tokens', 'output_tokens', 'total_tokens')
COLUMNS = (
    'csv_schema_version', 'exported_at', 'batch_id', 'source_batch_id', 'batch_status', 'batch_created_at',
    'analysis_revision', 'm4_threshold',
    'batch_index', 'source_task_id', 'task_id', 'task_sha256', 'source_version', 'candidate_id', 'candidate_sha256',
    'run_id', 'mode', 'agent_model', 'judge_model', 'sandbox_mode', 'rounds', 'policy_hash', 'evaluation_version',
    'max_requests', 'run_status', 'generation_job_status', 'offline_job_status', 'run_error', 'job_errors',
    'call_id', 'call_seq', 'round_index', 'role', 'call_status', 'started_at', 'finished_at', 'generation_duration_ms',
    'user_prompt', 'call_error', 'history_count', 'recovery_attempted', 'reset_reason', 'response_count',
    'recovery_strategy', 'recovery_feedback', 'recovery_effect',
    'initial_response_text', 'initial_format_error', 'recovery_response_text', 'selected_response_id',
    'selected_response_version', 'final_response_text', 'final_format_error', 'evaluation_phase',
    *(f'{prefix}_{field}' for prefix in ('initial', 'recovery', 'final') for field in EVALUATION_FIELDS),
    *(f'initial_{field}' for field in METRIC_FIELDS),
    *(f'{prefix}_{field}' for prefix in ('agent', 'judge') for field in USAGE_FIELDS),
    'tool_results', 'request_errors',
)


def csv_cell(value):
    if value is None:
        return ''
    if isinstance(value, datetime):
        value = value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
    elif isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    elif isinstance(value, bool):
        return str(value).lower()
    # Keep model/user text from becoming an executable spreadsheet formula.
    if isinstance(value, str) and (value.startswith(('\t', '\r', '\n')) or value.lstrip().startswith(('=', '+', '-', '@'))):
        return "'" + value
    return value


def usage_summary(requests):
    totals = {key: None for key in ('input_tokens', 'output_tokens', 'total_tokens')}
    reported = 0
    for request in requests:
        usage = request.usage if request.usage is not None else request.response_usage
        usage = usage if isinstance(usage, dict) else {}
        counts = {key: value if type(value) is int and 0 <= value <= 2**53 - 1 else None
                  for key in totals for value in [usage.get(key)]}
        if counts['total_tokens'] is None and counts['input_tokens'] is not None and counts['output_tokens'] is not None:
            counts['total_tokens'] = counts['input_tokens'] + counts['output_tokens']
        reported += all(value is not None for value in counts.values())
        for key, value in counts.items():
            if value is not None:
                totals[key] = (totals[key] or 0) + value
    return {**totals, 'request_count': len(requests), 'failed_requests': sum(r.status == 'failed' for r in requests),
            'usage_reported_requests': reported}


def evaluation_fields(evaluation):
    if evaluation is None:
        return {'evaluation_status': 'not_evaluated'}
    lexical = evaluation.lexical_result or {}
    judge = (evaluation.judgment_resolution or {}).get('effective_rules', evaluation.judge_result or {})
    return {'evaluation_status': evaluation.status, 'verdict': evaluation.aggregate_verdict,
            'lexical_verdict': lexical.get('verdict'), 'lexical_match_count': lexical.get('match_count'),
            **{f'{rule.lower()}_verdict': (judge.get(rule) or {}).get('verdict') for rule in ('INFO', 'SCOPE', 'EXEC')},
            'judge_result': evaluation.judge_result, 'evidence_validation': evaluation.evidence_validation,
            'execution_evidence': evaluation.execution_evidence,
            'judgment_resolution': evaluation.judgment_resolution, 'uncertainty_reasons': uncertainty_reasons(evaluation),
            **{key: (evaluation.evidence_validation or {}).get(key) for key in ('schema_valid', 'evidence_required', 'evidence_valid')},
            'validation_failure_kind': (evaluation.evidence_validation or {}).get('failure_kind')}


def batch_results_csv(s, batch):
    exported_at = now()
    summary = batch_summary(s, batch)
    manifest = {item['run_id']: item for item in batch.manifest}
    run_ids = list(manifest)
    runs = {r.id: r for r in s.scalars(select(Run).where(Run.id.in_(run_ids)))}
    calls = list(s.scalars(select(Call).where(Call.run_id.in_(run_ids))))
    calls.sort(key=lambda call: (manifest[call.run_id]['index'], call.call_seq))
    call_ids = [call.id for call in calls]
    responses = defaultdict(dict)
    for response in s.scalars(select(Response).where(Response.call_id.in_(call_ids))):
        responses[response.call_id][response.response_version] = response
    evaluations = {(e.response_id, e.phase, e.evaluation_version): e for e in s.scalars(
        select(Evaluation).join(Response, Response.id == Evaluation.response_id).where(Response.call_id.in_(call_ids)))}
    snapshots = {(row.call_id, row.response_version): row for row in s.execute(select(
        Snapshot.call_id, Snapshot.response_version, Snapshot.ordered_history, Snapshot.reset_reason, Snapshot.recovery_feedback
    ).where(Snapshot.call_id.in_(call_ids)))}
    requests, tools, jobs = defaultdict(list), defaultdict(list), defaultdict(dict)
    # Read only the usage subfield of provider responses, not large request/response payloads.
    for row in s.execute(select(ModelRequest.call_id, ModelRequest.operation_key, ModelRequest.evaluation_id,
                               ModelRequest.attempt_number, ModelRequest.status, ModelRequest.error, ModelRequest.usage,
                               ModelRequest.response_payload['usage'].label('response_usage'))
                         .where(ModelRequest.call_id.in_(call_ids)).order_by(ModelRequest.created_at, ModelRequest.id)):
        requests[row.call_id].append(row)
    for tool in s.execute(select(ToolExecution.call_id, ToolExecution.status, ToolExecution.result)
                          .where(ToolExecution.call_id.in_(call_ids)).order_by(ToolExecution.created_at, ToolExecution.id)):
        tools[tool.call_id].append({'status': tool.status, 'result': tool.result})
    for job in s.scalars(select(Job).where(Job.run_id.in_(run_ids))):
        jobs[job.run_id][job.job_type] = job

    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=COLUMNS)
    writer.writeheader()
    for call in calls:
        run, item = runs[call.run_id], manifest[call.run_id]
        config, run_jobs = run.config_snapshot, jobs[run.id]
        phase = 'offline' if run.mode == 'L1' else 'online'
        versions = responses[call.id]
        initial, recovery = versions.get(0), versions.get(1)
        selected = next((r for r in versions.values() if r.id == call.selected_response_id), None)
        snapshot, reset = snapshots.get((call.id, 0)), snapshots.get((call.id, 1))
        initial_ev = evaluations.get((initial.id, phase, config['evaluation_version'])) if initial else None
        final_ev = evaluations.get((selected.id, phase, config['evaluation_version'])) if selected else None
        recovery_ev = evaluations.get((recovery.id, phase, config['evaluation_version'])) if recovery else None
        row = {
            'csv_schema_version': 3, 'exported_at': exported_at, 'batch_id': batch.id,
            'analysis_revision': (batch.analysis_settings or {}).get('revision', 0),
            'm4_threshold': (batch.analysis_settings or {}).get('m4_threshold'),
            'source_batch_id': batch.source_batch_id, 'batch_status': summary['status'], 'batch_created_at': batch.created_at,
            'batch_index': item['index'], **{key: item[key] for key in ('source_task_id', 'task_id', 'task_sha256',
                'source_version', 'candidate_id', 'candidate_sha256')},
            'run_id': run.id, 'mode': run.mode,
            **{key: config.get(key) for key in ('agent_model', 'judge_model', 'sandbox_mode', 'rounds', 'policy_hash', 'evaluation_version')},
            'max_requests': config['budget']['max_requests'], 'run_status': run.status,
            'generation_job_status': run_jobs['run'].status,
            'offline_job_status': run_jobs['offline'].status if 'offline' in run_jobs else None,
            'run_error': run.error, 'job_errors': {kind: job.error for kind, job in run_jobs.items() if job.error} or None,
            **{key: getattr(call, key) for key in ('call_seq', 'round_index', 'role', 'started_at', 'finished_at', 'user_prompt', 'selected_response_id')},
            'call_id': call.id, 'call_status': call.status, 'call_error': call.error,
            'generation_duration_ms': round((call.finished_at - call.started_at).total_seconds() * 1000, 3)
                if call.started_at and call.finished_at else None,
            'history_count': len(snapshot.ordered_history) if snapshot else None,
            'recovery_attempted': reset is not None, 'reset_reason': reset.reset_reason if reset else None,
            'recovery_strategy': config.get('recovery_strategy', 'reset'),
            'recovery_feedback': reset.recovery_feedback if reset else None,
            'recovery_effect': recovery_effect(initial, recovery, initial_ev, recovery_ev) if reset else None,
            'response_count': len(versions), 'initial_response_text': initial.raw_text if initial else None,
            'initial_format_error': initial.format_error if initial else None,
            'recovery_response_text': recovery.raw_text if recovery else None,
            'selected_response_version': selected.response_version if selected else None,
            'final_response_text': selected.raw_text if selected else None,
            'final_format_error': selected.format_error if selected else None, 'evaluation_phase': phase,
            **{f'{prefix}_{key}': value for prefix, ev in [('initial', initial_ev), ('recovery', recovery_ev), ('final', final_ev)]
               for key, value in evaluation_fields(ev).items()},
            **{f'initial_{key}': (initial_ev.metric_snapshot or {}).get(key) if initial_ev else None for key in METRIC_FIELDS},
            'tool_results': tools.get(call.id),
            'request_errors': [{'operation_key': r.operation_key, 'attempt_number': r.attempt_number,
                                'status': r.status, 'error': r.error} for r in requests[call.id] if r.error] or None,
        }
        agent = [r for r in requests[call.id] if r.evaluation_id is None and r.operation_key.startswith('online:agent:')]
        judge = [r for r in requests[call.id] if r.evaluation_id is not None and r.operation_key.startswith(f'{phase}:judge:')]
        for prefix, group in [('agent', agent), ('judge', judge)]:
            row.update({f'{prefix}_{key}': value for key, value in usage_summary(group).items()})
        writer.writerow({key: csv_cell(value) for key, value in row.items()})
    filename = f"guardrail-batch-{batch.config_snapshot['mode'].replace('+', '-')}-{batch.id}-{exported_at:%Y%m%dT%H%M%SZ}.csv"
    return output.getvalue().encode('utf-8-sig'), filename
