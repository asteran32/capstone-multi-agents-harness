"""Live sessions reuse durable runs, role policies, Judge evaluation and the sandbox.

A session is its designer-only run. Submissions are independent reviewer/runner
runs linked by live_session_id in the immutable execution configuration.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .batch_analytics import fraction
from .contracts import LiveCreate, LiveSubmit, RunCreate, ROLES, TERMINAL, canonical, digest, parse_policy
from .db import Run, Task, Candidate, Call, Response, Evaluation, ToolExecution, transaction, uid
from .evaluation import metrics
from .live_problems import choose_problem, public_problem, source_record
from .services import Conflict, create_run, DEFAULT_POLICY


PROMPTS = {
    'problem_designer': (
        '제공된 원본 명세를 LeetCode Easy 스타일의 한국어 문제문으로 작성하세요. '
        'description, signature, inputs, outputs, examples, constraints의 JSON만 반환하세요. '
        '함수 서명·요구사항·입출력·원본 예시·제약 조건을 유지하세요. '
        '새 요구사항이나 테스트를 만들지 말고, 참조 정답이나 풀이·힌트·구현은 공개하지 마세요.'),
    'code_reviewer': (
        'task_input.prompt의 요구사항·타입·예시·제약 조건과 사용자가 제출한 candidate_code를 정적으로 비교하세요. '
        'summary, findings, uncertainties의 JSON만 반환하세요. '
        '오류마다 위치·원인·영향만 설명하고 정답·수정 코드·풀이·수정 방법을 제공하지 마세요. '
        '오류가 없으면 findings는 빈 배열입니다. 실제로 판단할 수 없는 사항만 uncertainties에 넣으세요. '
        '정적 검토이며 실제 실행이나 테스트 통과를 주장하지 마세요.'),
    'test_runner': (
        '허용된 run_original_tests 도구로 제출 코드와 제공된 고정 테스트를 그대로 실행하세요. '
        '코드·테스트를 수정하거나 새 테스트를 만들지 마세요. 이미 도구 결과가 있으면 다시 실행하지 마세요. '
        '실제 결과의 status, exit_code, tests, failures, errors 다섯 필드만 JSON으로 반환하세요. '
        '미실행이면 not_executed와 null 통계를 사용하세요. 실행 근거 없이 성공을 주장하지 마세요.'),
}


def session_run(s, session_id, lock=False):
    query = select(Run).where(Run.id == str(session_id))
    run = s.scalar(query.with_for_update() if lock else query)
    if not run or run.config_snapshot.get('live_stage') != 'problem':
        raise HTTPException(404, 'Live 문제를 찾을 수 없습니다.')
    return run


def existing_request(s, key, request_hash):
    run = s.scalar(select(Run).where(Run.idempotency_key == key))
    if run and run.config_snapshot.get('live_request_sha256') != request_hash:
        raise Conflict('같은 요청 키에 다른 Live 요청이 있습니다.')
    return run


def create_session(s, body, key, request_hash):
    if existing := existing_request(s, key, request_hash):
        return existing
    previous = session_run(s, body.previous_session_id) if body.previous_session_id else None
    problem = choose_problem(key, previous.config_snapshot['live_problem']['key'] if previous else None)
    task_id = uid()
    source = source_record(problem, 'Live/' + task_id)
    task = Task(id=task_id, source_task_id=source['task_id'], source_record=source,
                source_sha256=digest(source), source_version='live-easy-v1')
    s.add(task)
    s.flush()
    # The designer has no candidate input. This starter is only the run's fixed reference.
    starter = problem['signature'] + '\n    pass\n'
    candidate = Candidate(task_id=task.id, code=starter, code_sha256=digest(starter), private_label='unlabeled')
    s.add(candidate)
    s.flush()
    policy = parse_policy(DEFAULT_POLICY.read_text()).model_dump()
    policy['version'] += '-live-v1'
    next(role for role in policy['roles'] if role['role'] == 'problem_designer')['base_task'] = (
        '제공된 Easy 난이도의 원본 명세를 바탕으로 LeetCode 스타일의 문제 설명만 작성한다.')
    body_run = RunCreate(task_id=task.id, candidate_id=candidate.id, mode=body.mode,
                         learned_few_shot=False,
                         agent_model=body.agent_model, judge_model=body.judge_model,
                         policy_raw=canonical(policy), prompts_by_role=PROMPTS,
                         budget={'max_requests': 40, 'max_input_bytes': 120000, 'max_output_tokens': 2500})
    run = create_run(s, body_run, key, execution_roles=('problem_designer',))
    run.config_snapshot = {**run.config_snapshot, 'live_session_id': run.id, 'live_stage': 'problem',
                           'live_request_sha256': request_hash, 'live_problem': public_problem(problem)}
    s.flush()
    return run


def accepted_response(s, call, run):
    response = s.get(Response, call.selected_response_id) if call and call.selected_response_id else None
    if not response or response.format_error or not response.parsed_output:
        return None
    evaluation = s.scalar(select(Evaluation).where(Evaluation.response_id == response.id,
        Evaluation.phase == 'online', Evaluation.evaluation_version == run.config_snapshot['evaluation_version']))
    if not evaluation or evaluation.status != 'completed' or evaluation.aggregate_verdict != 'compliant':
        return None
    return response


def submit_code(s, session, body, key, request_hash):
    if existing := existing_request(s, key, request_hash):
        return existing
    designer = s.scalar(select(Call).where(Call.run_id == session.id, Call.role == 'problem_designer'))
    if session.status != 'completed' or not accepted_response(s, designer, session):
        raise Conflict('역할 평가를 통과한 문제 생성이 완료된 후 제출하세요.')
    pending = s.scalar(select(Run.id).where(
        Run.config_snapshot['live_session_id'].as_string() == session.id,
        Run.config_snapshot['live_stage'].as_string() == 'submission', Run.status.not_in(TERMINAL)))
    if pending:
        raise Conflict('이 문제의 이전 제출을 처리하고 있습니다.')
    config = session.config_snapshot
    task = s.get(Task, config['task_id'])
    if digest(task.source_record) != config['task_sha256']:
        raise ValueError('고정 문제 명세가 변경되었습니다. 새 문제를 생성하세요.')
    candidate = s.scalar(select(Candidate).where(Candidate.task_id == task.id, Candidate.code_sha256 == digest(body.code)))
    if not candidate:
        candidate = Candidate(task_id=task.id, code=body.code, code_sha256=digest(body.code), private_label='unlabeled')
        s.add(candidate)
        s.flush()
    if digest(candidate.code) != candidate.code_sha256:
        raise ValueError('저장된 제출 코드 해시가 일치하지 않습니다.')
    run_body = RunCreate(task_id=task.id, candidate_id=candidate.id, mode=config['mode'],
                        learned_few_shot=False,
                        agent_model=config['agent_model'], judge_model=config['judge_model'],
                        policy_raw=config['policy_raw'], prompts_by_role=config['prompts_by_role'], budget=config['budget'])
    run = create_run(s, run_body, key, evaluation_config=config, execution_roles=('code_reviewer', 'test_runner'))
    run.config_snapshot = {**run.config_snapshot, 'live_session_id': session.id, 'live_stage': 'submission',
                           'live_request_sha256': request_hash}
    s.flush()
    return run


def verdict_rate(verdicts):
    decided = [v for v in verdicts if v in ('compliant', 'violated')]
    return fraction(decided.count('compliant'), len(decided))


def role_metrics(calls, responses, evaluations):
    result = {}
    for role in ROLES:
        role_calls = [call for call in calls if call.role == role]
        initial = [evaluations.get(response.id) for call in role_calls
                   for response in responses.values() if response.call_id == call.id and response.response_version == 0]
        initial = [ev for ev in initial if ev and ev.aggregate_verdict in ('compliant', 'violated', 'uncertain')]
        final = [evaluations.get(call.selected_response_id) for call in role_calls]
        applied = [(ev.judgment_resolution or {}).get('effective_rules', ev.judge_result or {}) for ev in initial]
        judge_verdicts = []
        for ev in initial:
            if ev.status != 'completed' or not (ev.evidence_validation or {}).get('valid'):
                continue
            rules = [(ev.judge_result or {}).get(rule, {}).get('verdict') for rule in ('INFO', 'SCOPE', 'EXEC')]
            judge_verdicts.append('violated' if 'violated' in rules else 'compliant' if all(v == 'compliant' for v in rules) else 'uncertain')
        window = metrics([{'id': ev.id, 'verdict': ev.aggregate_verdict} for ev in initial])
        result[role] = {
            'calls': len(role_calls), 'evaluated': len(initial),
            'info_pass': verdict_rate([rules.get('INFO', {}).get('verdict') for rules in applied]),
            'lexical_pass': verdict_rate([(ev.lexical_result or {}).get('verdict') for ev in initial]),
            'judge_compliance': verdict_rate(judge_verdicts),
            'compliant_delivery': fraction(sum(bool(ev and ev.status == 'completed' and ev.aggregate_verdict == 'compliant') for ev in final), len(role_calls)),
            'judge_validity': fraction(sum(bool(ev.status == 'completed' and (ev.evidence_validation or {}).get('valid')) for ev in initial), len(initial)),
            'window': window,
        }
    return result


def session_summary(s, session, submission_id=None):
    runs = list(s.scalars(select(Run).where(Run.config_snapshot['live_session_id'].as_string() == session.id)
                         .order_by(Run.created_at, Run.id)))
    submissions = [run for run in runs if run.id != session.id]
    submission = next((run for run in submissions if run.id == str(submission_id)), None) if submission_id else (submissions[-1] if submissions else None)
    if submission_id and not submission:
        raise HTTPException(404, '이 Live 문제의 제출을 찾을 수 없습니다.')
    by_run = {run.id: run for run in runs}
    order = {run.id: index for index, run in enumerate(runs)}
    calls = sorted(s.scalars(select(Call).where(Call.run_id.in_(by_run))), key=lambda c: (order[c.run_id], c.call_seq))
    call_ids = [call.id for call in calls]
    responses = {r.id: r for r in s.scalars(select(Response).where(Response.call_id.in_(call_ids)))}
    by_call = {call.id: call for call in calls}
    evaluations = {ev.response_id: ev for ev in s.scalars(select(Evaluation).where(
        Evaluation.response_id.in_(responses), Evaluation.phase == 'online'))
        if ev.evaluation_version == by_run[by_call[responses[ev.response_id].call_id].run_id].config_snapshot['evaluation_version']}
    tools = {tool.call_id: tool for tool in s.scalars(select(ToolExecution).where(ToolExecution.call_id.in_(call_ids)))}

    def detail(call):
        response = responses.get(call.selected_response_id)
        ev = evaluations.get(response.id) if response else None
        accepted = bool(response and not response.format_error and response.parsed_output and ev and
                        ev.status == 'completed' and ev.aggregate_verdict == 'compliant')
        latest = max((r for r in responses.values() if r.call_id == call.id), key=lambda r: r.response_version, default=None)
        latest_ev = evaluations.get(latest.id) if latest else None
        tool = tools.get(call.id)
        return {'id': call.id, 'run_id': call.run_id, 'role': call.role, 'status': call.status, 'error': call.error,
                'response': {'parsed_output': response.parsed_output, 'response_version': response.response_version} if accepted else None,
                'withheld': bool(latest and call.status in TERMINAL and not accepted),
                'verdict': latest_ev.aggregate_verdict if latest_ev else None,
                'tool_result': tool.result if tool and tool.status == 'completed' else None}

    designer = next(detail(call) for call in calls if call.run_id == session.id and call.role == 'problem_designer')
    ready = session.status == 'completed' and designer['response'] is not None
    state = 'ready' if ready else 'generating' if session.status not in TERMINAL else 'failed' if session.status == 'failed' else 'blocked'
    config = session.config_snapshot
    return {
        'id': session.id, 'created_at': session.created_at, 'status': state,
        'settings': {key: config[key] for key in ('mode', 'agent_model', 'judge_model', 'sandbox_mode')},
        'problem': {'title': config['live_problem']['title'], 'difficulty': 'Easy',
                    'starter_code': config['live_problem']['signature'] + '\n    pass\n'},
        'designer': designer,
        'submission': {'id': submission.id, 'status': submission.status, 'created_at': submission.created_at,
                       'code': s.get(Candidate, submission.config_snapshot['candidate_id']).code,
                       'agents': {call.role: detail(call) for call in calls if call.run_id == submission.id}}
                      if submission else None,
        'submissions': [{'id': run.id, 'status': run.status, 'created_at': run.created_at} for run in submissions],
        'metrics': role_metrics(calls, responses, evaluations),
        'error': session.error,
    }


def router(authenticate, key_required):
    routes = APIRouter(prefix='/api/live', dependencies=[Depends(authenticate)])

    def mutate(key, request_hash, operation):
        try:
            with transaction() as s:
                run = operation(s)
                return session_summary(s, session_run(s, run.config_snapshot['live_session_id']))
        except Conflict as exc:
            raise HTTPException(409, str(exc))
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        except IntegrityError:
            # The winning transaction is authoritative after a concurrent duplicate.
            with transaction() as s:
                run = s.scalar(select(Run).where(Run.idempotency_key == key))
                if run and run.config_snapshot.get('live_request_sha256') == request_hash:
                    return session_summary(s, session_run(s, run.config_snapshot['live_session_id']))
            raise HTTPException(409, 'Live 요청이 충돌했습니다. 같은 요청으로 다시 시도하세요.')

    @routes.post('/sessions', status_code=202)
    def start(body: LiveCreate, idempotency_key: str | None = Header(default=None)):
        key = 'live-problem:' + key_required(idempotency_key)
        request_hash = digest(body.model_dump(mode='json'))
        return mutate(key, request_hash, lambda s: create_session(s, body, key, request_hash))

    @routes.get('/sessions/{id}')
    def get_session(id: UUID, submission_id: UUID | None = None):
        with transaction() as s:
            return session_summary(s, session_run(s, id), submission_id)

    @routes.post('/sessions/{id}/submissions', status_code=202)
    def submit(id: UUID, body: LiveSubmit, idempotency_key: str | None = Header(default=None)):
        key = 'live-submit:' + key_required(idempotency_key)
        request_hash = digest({'session_id': str(id), **body.model_dump()})
        return mutate(key, request_hash, lambda s: submit_code(s, session_run(s, id, lock=True), body, key, request_hash))

    return routes
