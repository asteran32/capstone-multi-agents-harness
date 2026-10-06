"""Read-only batch observations. Analysis thresholds never drive the execution graph."""
from collections import Counter, defaultdict
from math import floor, isfinite
from statistics import mean

from sqlalchemy import select

from .batches import batch_summary
from .contracts import ROLES, TERMINAL
from .db import Call, Evaluation, Job, Log, ModelRequest, Response, Run, Snapshot, ToolExecution, now
from .exports import usage_summary
from .evaluation_v2 import recovery_effect, uncertainty_reasons

VERDICTS = ('violated', 'compliant', 'uncertain')
FAILURE_KINDS = ('schema', 'evidence_missing', 'evidence_mismatch', 'authority_conflict', 'unexpected_evidence', 'provider', 'budget', 'unknown')


def fraction(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
            'rate': numerator / denominator if denominator else None}


def analysis_settings(batch):
    return {'m4_threshold': None, 'revision': 0, 'updated_at': None, 'history': [], **(batch.analysis_settings or {})}


def distribution(observations):
    observations = [(value, location) for value, location in observations
                    if isinstance(value, (float, int)) and not isinstance(value, bool) and isfinite(value)]
    if not observations:
        return {'count': 0, 'min': None, 'max': None, 'mean': None, 'median': None, 'p95': None,
                'min_at': None, 'max_at': None}
    values = sorted(value for value, _ in observations)
    def percentile(p):
        index = (len(values) - 1) * p
        lower = floor(index)
        return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (index - lower)
    low, high = min(observations, key=lambda item: item[0]), max(observations, key=lambda item: item[0])
    return {'count': len(values), 'min': low[0], 'max': high[0], 'mean': mean(values),
            'median': percentile(.5), 'p95': percentile(.95), 'min_at': low[1], 'max_at': high[1]}


def verdict_summary(evaluations):
    counts = Counter(e.aggregate_verdict for e in evaluations if e and e.aggregate_verdict in VERDICTS)
    v, c, u = (counts[key] for key in VERDICTS)
    return {'V': v, 'C': c, 'U': u, 'violation': fraction(v, v + c), 'coverage': fraction(v + c, v + c + u)}


def batch_analytics(s, batch):
    summary = batch_summary(s, batch)
    config = analysis_settings(batch)
    threshold = config['m4_threshold']
    phase = 'offline' if summary['mode'] == 'L1' else 'online'
    manifest = {item['run_id']: item for item in batch.manifest}
    run_ids = list(manifest)
    runs = {r.id: r for r in s.scalars(select(Run).where(Run.id.in_(run_ids)))}
    calls = sorted(s.scalars(select(Call).where(Call.run_id.in_(run_ids))),
                   key=lambda c: (manifest[c.run_id]['index'], c.call_seq))
    call_ids = [c.id for c in calls]
    by_id = {c.id: c for c in calls}
    responses = defaultdict(dict)
    for response in s.execute(select(Response.id, Response.call_id, Response.response_version, Response.format_error, Response.response_sha256)
                              .where(Response.call_id.in_(call_ids))):
        responses[response.call_id][response.response_version] = response
    response_by_id = {r.id: r for versions in responses.values() for r in versions.values()}
    evaluations = {e.response_id: e for e in s.scalars(select(Evaluation).join(Response, Response.id == Evaluation.response_id)
        .where(Response.call_id.in_(call_ids), Evaluation.phase == phase))
        if e.evaluation_version == runs[by_id[response_by_id[e.response_id].call_id].run_id].config_snapshot['evaluation_version']}
    snapshots = {row.id: row for row in s.execute(select(Snapshot.id, Snapshot.call_id, Snapshot.response_version)
                                                  .where(Snapshot.call_id.in_(call_ids)))}
    recovery_calls = {row.call_id for row in snapshots.values() if row.response_version == 1}
    requests = list(s.execute(select(ModelRequest.id, ModelRequest.call_id, ModelRequest.input_snapshot_id,
        ModelRequest.evaluation_id, ModelRequest.operation_key, ModelRequest.attempt_number, ModelRequest.created_at,
        ModelRequest.status, ModelRequest.error, ModelRequest.usage,
        ModelRequest.response_payload['usage'].label('response_usage'))
        .where(ModelRequest.call_id.in_(call_ids)).order_by(ModelRequest.created_at, ModelRequest.id)))
    tools = list(s.execute(select(ToolExecution.call_id, ToolExecution.status, ToolExecution.result)
                          .where(ToolExecution.call_id.in_(call_ids))))
    logs = list(s.scalars(select(Log).where(Log.call_id.in_(call_ids),
                                           Log.node_name.in_(['judge_validation', 'model_request']))
                         .order_by(Log.call_id, Log.event_seq)))
    jobs = list(s.scalars(select(Job).where(Job.run_id.in_(run_ids), Job.job_type.in_(['run', 'offline']))))

    def location(call, version=0):
        return {'run_id': call.run_id, 'call_id': call.id, 'source_task_id': manifest[call.run_id]['source_task_id'],
                'batch_index': manifest[call.run_id]['index'], 'call_seq': call.call_seq,
                'round_index': call.round_index, 'role': call.role, 'phase': phase, 'response_version': version}

    events = []
    def event(kind, call, detail, version=0, **values):
        events.append({'id': f'{kind}:{call.id}:{version}:{len(events)}', 'kind': kind,
                       **location(call, version), 'detail': detail, **values})

    initial_evs, final_evs = [], []
    generated = selected = compliant = terminal_unselected = 0
    initial_states = Counter()
    recoveries = Counter()
    recovery_kinds, recovery_strategies, comparisons = Counter(), defaultdict(Counter), Counter()
    unresolved_reasons, conflict_counts = Counter(), Counter()
    uncertainty_counts = {scope: Counter() for scope in ('initial', 'recovery', 'latest')}
    role_data = {role: {'initial': [], 'final': [], 'm2': [], 'm4': [], 'coverage': [], 'warmup': 0,
                        'missing_window': 0, 'calls': 0, 'rules': {rule: Counter() for rule in ('INFO', 'SCOPE', 'EXEC')}} for role in ROLES}
    threshold_observations, sequences = [], defaultdict(list)
    call_times, queue_times = [], []
    runner_reports, matching_reports, matching_unexecuted = 0, 0, 0
    for call in calls:
        loc = location(call)
        versions = responses[call.id]
        initial = versions.get(0)
        final = response_by_id.get(call.selected_response_id)
        first_ev = evaluations.get(initial.id) if initial else None
        final_ev = evaluations.get(final.id) if final else None
        generated += initial is not None
        selected += final is not None
        terminal_unselected += call.status in TERMINAL and final is None
        compliant += bool(final and final_ev and final_ev.status == 'completed' and final_ev.aggregate_verdict == 'compliant')
        initial_evs.append(first_ev)
        final_evs.append(final_ev)
        initial_states[first_ev.status if first_ev else 'not_evaluated' if initial else 'not_generated'] += 1
        data = role_data[call.role]
        data['calls'] += 1
        data['initial'].append(first_ev)
        data['final'].append(final_ev)
        if first_ev:
            lex, metric = first_ev.lexical_result or {}, first_ev.metric_snapshot or {}
            data['m2'].append((lex.get('match_count'), loc))
            data['coverage'].append((metric.get('decision_coverage'), loc))
            if first_ev.aggregate_verdict in VERDICTS:
                for rule, counter in data['rules'].items():
                    applied = (first_ev.judgment_resolution or {}).get('effective_rules', first_ev.judge_result or {})
                    verdict = (applied.get(rule) or {}).get('verdict')
                    if verdict in VERDICTS:
                        counter[verdict] += 1
            if first_ev.aggregate_verdict == 'violated':
                event('violation', call, '최초 응답 자동 위반', verdict='violated',
                      evidence={'lexical': lex.get('match_count'), 'judge': first_ev.judge_result,
                                'execution': first_ev.execution_evidence})
        else:
            metric = {}
        warmup = bool(metric and (metric.get('warmup') or metric.get('window_count', 0) < 5))
        data['warmup'] += warmup
        value = metric.get('role_violation_rate')
        eligible = bool(metric and not warmup and metric.get('window_count', 0) >= 5 and value is not None)
        if eligible:
            data['m4'].append((value, loc))
            threshold_observations.append((value, loc))
        elif not warmup:
            data['missing_window'] += 1
        sequences[(call.run_id, call.role)].append((call, value if eligible else None))
        if call.id in recovery_calls:
            recovery = versions.get(1)
            recovery_ev = evaluations.get(recovery.id) if recovery else None
            if call.status not in TERMINAL:
                outcome = 'running'
            elif final and final.response_version == 1 and final_ev and final_ev.status == 'completed' and final_ev.aggregate_verdict == 'compliant':
                outcome = 'success'
            elif call.status == 'unresolved':
                outcome = 'unresolved'
            else:
                outcome = 'failed'
            recoveries[outcome] += 1
            strategy = runs[call.run_id].config_snapshot.get('recovery_strategy', 'reset')
            recovery_strategies[strategy]['triggered'] += 1
            recovery_strategies[strategy][outcome] += 1
            effect = recovery_effect(initial, recovery, first_ev, recovery_ev)
            comparisons['same_valid_pairs'] += effect['same_response'] and effect['valid_pair']
            comparisons['changed_verdict'] += effect['same_response_verdict_changed']
            comparisons['response_changed'] += effect['response_changed']
            comparisons['error_resolved'] += effect['evaluation_error_resolved']
            if outcome == 'success':
                recovery_kinds[effect['success_kind']] += 1
            event('recovery', call, 'L3 복구', 1, outcome=outcome,
                  strategy=strategy, effect=effect,
                  initial_verdict=first_ev.aggregate_verdict if first_ev else None,
                  recovery_verdict=recovery_ev.aggregate_verdict if recovery_ev else None)
        latest = versions[max(versions)] if versions else None
        latest_ev = evaluations.get(latest.id) if latest else None
        for scope, ev in [('initial', first_ev), ('recovery', evaluations.get(versions[1].id) if 1 in versions else None), ('latest', latest_ev)]:
            if ev and ev.aggregate_verdict == 'uncertain':
                uncertainty_counts[scope]['evaluations'] += 1
                uncertainty_counts[scope].update(uncertainty_reasons(ev) or ['unknown'])
        if call.status == 'unresolved':
            reasons = uncertainty_reasons(latest_ev)
            cause = 'violation_persisted' if latest_ev and latest_ev.aggregate_verdict == 'violated' else \
                next((r for r in ('judge_error', 'judgment_conflict', 'server_check_uncertainty', 'execution_evidence', 'semantic_uncertainty') if r in reasons), 'unknown')
            unresolved_reasons[cause] += 1
            event('unresolved', call, '복구 후 응답 미선택', latest.response_version if latest else 1,
                  primary_cause=cause, contributing_causes=reasons, verdict=latest_ev.aggregate_verdict if latest_ev else None)
        if call.started_at and call.finished_at:
            duration = (call.finished_at - call.started_at).total_seconds() * 1000
            if duration >= 0:
                call_times.append((duration, {**loc, 'status': call.status}))
        if call.call_seq == 1 and call.started_at:
            duration = (call.started_at - runs[call.run_id].created_at).total_seconds() * 1000
            if duration >= 0:
                queue_times.append((duration, loc))
        if call.error:
            event('call_error', call, (call.error or {}).get('message') or (call.error or {}).get('code', '호출 오류'), error=call.error)
        for response in versions.values():
            if response.format_error:
                event('format_error', call, response.format_error, response.response_version)
        if call.role == 'test_runner' and final_ev and (final_ev.execution_evidence or {}).get('applicable') is not False:
            evidence = final_ev.execution_evidence or {}
            if evidence.get('reported'):
                runner_reports += 1
                matches = evidence.get('report_match', evidence.get('verdict') == 'compliant')
                matching_reports += matches
                matching_unexecuted += matches and (evidence.get('actual') or {}).get('status') == 'not_executed'
            report_verdict = evidence.get('checks', {}).get('runner_report', {}).get('verdict', evidence.get('verdict'))
            if report_verdict == 'violated':
                event('tool_mismatch', call, 'Runner 보고와 도구 결과 불일치', final.response_version, evidence=evidence)

    exceeded, episodes, longest, first_exceedance, affected = 0, 0, 0, None, set()
    if threshold is not None:
        for observations in sequences.values():
            streak, previous_round = 0, None
            for call, value in observations:
                if previous_round is None or call.round_index != previous_round + 1:
                    streak = 0
                if value is not None and value > threshold:
                    exceeded += 1
                    start = streak == 0
                    episodes += start
                    streak += 1
                    longest = max(longest, streak)
                    affected.add(call.run_id)
                    first_exceedance = first_exceedance or location(call)
                    event('threshold', call, 'M4 threshold 초과', value=value, threshold=threshold,
                          episode_start=start, streak=streak)
                else:
                    streak = 0
                previous_round = call.round_index

    validation_logs = defaultdict(dict)
    for entry in logs:
        details = entry.details or {}
        if entry.node_name == 'judge_validation' and details.get('evaluation_id') and details.get('attempt'):
            validation_logs[details['evaluation_id']][details['attempt']] = details
    validation = Counter({key: 0 for key in ('evaluations_with_attempts', 'first_attempts', 'missing_first_attempts',
        'first_passes', 'retried_evaluations', 'retry_success', 'attempts', 'schema_checks', 'schema_failures',
        'evidence_checks', 'evidence_failures', 'classified_attempts', 'final_errors', 'finished_evaluations')})
    failure_counts = Counter({kind: 0 for kind in FAILURE_KINDS})
    for ev in evaluations.values():
        response = response_by_id[ev.response_id]
        call = by_id[response.call_id]
        saved = ev.evidence_validation or {}
        resolution = ev.judgment_resolution or {}
        contract_failures = {key: value for key, value in (ev.execution_evidence or {}).get('checks', {}).items()
                             if key != 'runner_report' and value.get('verdict') == 'violated'}
        if contract_failures:
            event('server_violation', call, '역할 출력 규격 또는 고정 실행 입력 위반', response.response_version,
                  checks=contract_failures)
        if ev.status in ('completed', 'error') and (call.role == 'test_runner' or (ev.execution_evidence or {}).get('schema_version') == 3):
            conflict_counts['eligible_evaluations'] += 1
        if resolution.get('conflict_checked'):
            conflict_counts['checked'] += 1
            if resolution.get('conflicts'):
                conflict_counts['conflicting'] += 1
                conflict_counts['initial' if response.response_version == 0 else 'recovery'] += 1
                event('judgment_conflict', call, '서버 검사와 Judge 판정 충돌', response.response_version,
                      conflicts=resolution['conflicts'], effective_rules=resolution['effective_rules'],
                      judge_result=ev.judge_result, verdict=ev.aggregate_verdict)
        if ev.aggregate_verdict == 'uncertain':
            event('uncertainty', call, '불확실 판정', response.response_version, reasons=uncertainty_reasons(ev) or ['unknown'])
        attempts = dict(validation_logs[ev.id])
        if saved.get('attempt'):
            attempts[saved['attempt']] = {key: value for key, value in saved.items() if key != 'attempts'}
        for attempt in saved.get('attempts', []):
            attempts[attempt['attempt']] = attempt
        if attempts:
            validation['evaluations_with_attempts'] += 1
            validation['first_attempts'] += 1 in attempts
            validation['missing_first_attempts'] += 1 not in attempts
            validation['first_passes'] += attempts.get(1, {}).get('valid') is True
            validation['retried_evaluations'] += max(attempts) > 1
            validation['retry_success'] += (saved.get('valid') is True and any(a.get('valid') is False for a in attempts.values()))
        for attempt_number, attempt in sorted(attempts.items()):
            validation['attempts'] += 1
            validation['schema_checks'] += attempt.get('schema_valid') is not None
            validation['schema_failures'] += attempt.get('schema_valid') is False
            validation['evidence_checks'] += attempt.get('evidence_required') is True and attempt.get('evidence_valid') is not None
            validation['evidence_failures'] += attempt.get('evidence_valid') is False
            validation['classified_attempts'] += attempt.get('schema_version', 1) >= 2 and attempt.get('failure_kind') != 'unknown'
            if attempt.get('valid') is False:
                kind = attempt.get('failure_kind') or 'unknown'
                kind = kind if kind in FAILURE_KINDS else 'unknown'
                failure_counts[kind] += 1
                event('judge_validation', call, attempt.get('error') or 'Judge 검증 실패', response.response_version,
                      failure_kind=kind, attempt=attempt_number, evaluation_id=ev.id,
                      recovered=saved.get('valid') is True)
        if ev.status == 'error':
            validation['final_errors'] += 1
            event('evaluation_error', call, saved.get('error') or '최종 평가 오류', response.response_version)
        validation['finished_evaluations'] += ev.status in ('completed', 'error')

    request_groups = {
        'agent': [r for r in requests if r.evaluation_id is None and r.operation_key.startswith('online:agent:')],
        'judge_online': [r for r in requests if r.evaluation_id is not None and r.operation_key.startswith('online:judge:')],
        'judge_offline': [r for r in requests if r.evaluation_id is not None and r.operation_key.startswith('offline:judge:')],
        'recovery': [r for r in requests if r.input_snapshot_id in snapshots and snapshots[r.input_snapshot_id].response_version == 1],
    }
    cost = {key: {**usage_summary(group), 'transport_retries': sum(r.attempt_number > 1 for r in group)}
            for key, group in {**request_groups, 'total': requests}.items()}
    timings = defaultdict(list)
    pending = {}
    request_by_operation = {(r.call_id, r.operation_key, r.attempt_number): r for r in requests}
    for entry in logs:
        if entry.node_name != 'model_request':
            continue
        key = (entry.call_id, entry.operation_key)
        if entry.status == 'started':
            pending[key] = (entry.created_at, (entry.details or {}).get('attempt'))
        elif entry.status in ('completed', 'failed', 'retry') and key in pending:
            started, attempt = pending.pop(key)
            request = request_by_operation.get((*key, attempt))
            if request:
                duration = (entry.created_at - started).total_seconds() * 1000
                if duration >= 0:
                    loc = location(by_id[entry.call_id], snapshots[request.input_snapshot_id].response_version)
                    loc = {**loc, 'phase': request.operation_key.split(':', 1)[0], 'request_id': request.id}
                    group = 'agent' if request.evaluation_id is None else 'judge_' + loc['phase']
                    timings[group].append((duration, loc))
                    if snapshots[request.input_snapshot_id].response_version == 1:
                        timings['recovery'].append((duration, loc))
    tool_counts = Counter()
    for tool in tools:
        result = tool.result or {}
        if tool.status != 'completed':
            tool_counts['running_or_incomplete'] += 1
        elif result.get('mock') or result.get('status') == 'not_executed':
            tool_counts['not_executed'] += 1
        elif result.get('status') in ('pass', 'fail', 'error', 'timeout'):
            tool_counts['executed'] += 1
            tool_counts[result['status']] += 1
        else:
            tool_counts['unknown'] += 1
    for job in jobs:
        if job.error:
            call = next(c for c in calls if c.run_id == job.run_id)
            event('job_error', call, f'{job.job_type} 작업 오류', error=job.error)
    for run in runs.values():
        if run.error:
            call = next(c for c in calls if c.run_id == run.id)
            event('run_error', call, 'Run 오류', error=run.error)

    events.sort(key=lambda e: (e['batch_index'], e['call_seq'], e['response_version'], e['kind'], e.get('attempt', 0)))
    # Correct first-exceedance ordering across the three independent role sequences.
    threshold_events = [e for e in events if e['kind'] == 'threshold']
    if threshold_events:
        first_exceedance = {key: threshold_events[0][key] for key in location(calls[0])}
    call_states = Counter(c.status for c in calls)
    errors = Counter((c.error or {}).get('code', 'unknown') for c in calls if c.error)
    roles = []
    for role, data in role_data.items():
        roles.append({'role': role, 'planned': data['calls'], 'initial': verdict_summary(data['initial']),
                      'final': verdict_summary(data['final']), 'm2': distribution(data['m2']), 'm4': distribution(data['m4']),
                      'coverage': distribution(data['coverage']), 'warmup': data['warmup'], 'missing_window': data['missing_window'],
                      'rules': {rule: {key: counts[key] for key in VERDICTS} for rule, counts in data['rules'].items()}})
    return {
        'schema_version': 2, 'generated_at': now(), 'batch_id': batch.id, 'status': summary['status'], 'phase': phase,
        'analysis_settings': config, 'progress': {'runs': len(runs), 'finished_runs': summary['finished_runs'],
            'planned': len(calls), 'states': dict(call_states), 'generated': generated, 'selected': selected,
            'terminal_unselected': terminal_unselected, 'evaluation_states': dict(initial_states),
            'evaluation_completion': fraction(initial_states['completed'], generated)},
        'initial': verdict_summary(initial_evs), 'final': verdict_summary(final_evs),
        'compliant_delivery': fraction(compliant, len(calls)), 'roles': roles,
        'unresolved': {'rate': fraction(call_states['unresolved'], len(calls)), 'primary_causes': dict(unresolved_reasons)},
        'uncertainty': {scope: {'evaluations': counts['evaluations'], 'causes': {k: v for k, v in counts.items() if k != 'evaluations'}}
                        for scope, counts in uncertainty_counts.items()},
        'conflicts': {'rate': fraction(conflict_counts['conflicting'], conflict_counts['checked']),
                      'initial': conflict_counts['initial'], 'recovery': conflict_counts['recovery'],
                      'unmeasured': max(0, conflict_counts['eligible_evaluations'] - conflict_counts['checked'])},
        'threshold': {'value': threshold, 'eligible': len(threshold_observations),
            'warmup': sum(d['warmup'] for d in role_data.values()), 'missing': sum(d['missing_window'] for d in role_data.values()),
            'exceedances': exceeded if threshold is not None and threshold_observations else None,
            'episodes': episodes if threshold is not None and threshold_observations else None,
            'affected_problems': len(affected) if threshold is not None and threshold_observations else None,
            'longest_streak': longest if threshold is not None and threshold_observations else None,
            'first_at': first_exceedance, 'distribution': distribution(threshold_observations)},
        'recovery': {'enabled': summary['mode'] == 'FULL', 'triggered': len(recovery_calls),
            **{key: recoveries[key] for key in ('success', 'unresolved', 'failed', 'running')},
            'strategies': {key: {**dict(counts), 'success_rate': fraction(counts['success'], counts['triggered'] - counts['running'])}
                           for key, counts in recovery_strategies.items()},
            'configured_strategies': sorted({r.config_snapshot.get('recovery_strategy', 'reset') for r in runs.values()}),
            'success_kinds': dict(recovery_kinds), 'comparisons': dict(comparisons),
            'same_response_verdict_change': fraction(comparisons['changed_verdict'], comparisons['same_valid_pairs']),
            'success_rate': fraction(recoveries['success'], len(recovery_calls) - recoveries['running'])},
        'judge': {**dict(validation), 'failures': dict(failure_counts),
            'first_pass': fraction(validation['first_passes'], validation['first_attempts']),
            'schema_failure': fraction(validation['schema_failures'], validation['schema_checks']),
            'evidence_failure': fraction(validation['evidence_failures'], validation['evidence_checks']),
            'final_failure': fraction(validation['final_errors'], validation['finished_evaluations'])},
        'execution': {'tools': dict(tool_counts), 'runner_planned': role_data['test_runner']['calls'],
            'runner_report_match': fraction(matching_reports, runner_reports), 'matching_unexecuted': matching_unexecuted,
            'format_errors': sum(bool(r.format_error) for r in response_by_id.values()), 'call_errors': dict(errors),
            'failed_requests': sum(r.status == 'failed' for r in requests),
            'timeout_requests': sum('timeout' in str(r.error).lower() for r in requests if r.error)},
        'cost': cost,
        'timing': {'call_ms': distribution(call_times), 'queue_ms': distribution(queue_times),
                   **{key + '_ms': distribution(timings[key]) for key in ('agent', 'judge_online', 'judge_offline', 'recovery')}},
        'events': events, 'event_counts': dict(Counter(e['kind'] for e in events)),
    }
