import json
import pytest
from sqlalchemy import select, func
from langgraph.checkpoint.memory import InMemorySaver
from app import db, runtime
from app.contracts import ROLES, RunCreate, canonical, digest
from app.services import create_run, ensure_snapshot, select_history, Conflict
from app.graphs import agent_graph, offline_graph
from app.worker import invoke_or_resume
from app.providers import mock


def run_create(mode='L1+L2', rounds=1, prompt='review', max_requests=120):
    with db.transaction() as s:
        task = s.scalar(select(db.Task).order_by(db.Task.source_task_id))
        candidate = s.scalar(select(db.Candidate).where(db.Candidate.task_id == task.id, db.Candidate.private_label == 'correct'))
        body = RunCreate(task_id=task.id, candidate_id=candidate.id, mode=mode, rounds=rounds,
                         prompts_by_role={role: prompt for role in ROLES}, budget={'max_requests':max_requests})
        run = create_run(s, body, db.uid())
        s.flush()
        return run.id


def execute_run(run_id, graph=None):
    graph = graph or agent_graph(InMemorySaver())
    with db.transaction() as s:
        ids = list(s.scalars(select(db.Call.id).where(db.Call.run_id == run_id).order_by(db.Call.call_seq)))
    for call_id in ids:
        invoke_or_resume(graph, {'call_id':call_id}, call_id)
    return ids


@pytest.mark.parametrize('mode,l1,l2,l3',[('Base',False,False,False),('L1',True,False,False),('L1+L2',True,True,False),('FULL',True,True,True)])
def test_modes_and_payloads(database, mode, l1, l2, l3):
    ids = execute_run(run_create(mode, prompt='[mock:recover]'))
    with db.transaction() as s:
        for call_id in ids:
            call = s.get(db.Call, call_id)
            snap = s.scalar(select(db.Snapshot).where(db.Snapshot.call_id == call_id, db.Snapshot.response_version == 0))
            assert ('필수 지침' in snap.role_prompt) == l1
            evs = list(s.scalars(select(db.Evaluation).join(db.Response).where(db.Response.call_id == call_id)))
            assert all(e.status == 'disabled' for e in evs) == (not l2)
            judge_requests = list(s.scalars(select(db.ModelRequest).where(db.ModelRequest.call_id == call_id, db.ModelRequest.evaluation_id.is_not(None))))
            assert bool(judge_requests) == l2
            assert len(list(s.scalars(select(db.Response).where(db.Response.call_id == call_id)))) == (2 if l3 and call.role != 'test_runner' else 1)
            for req in s.scalars(select(db.ModelRequest).where(db.ModelRequest.call_id == call_id)):
                payload = canonical(req.request_payload)
                assert 'private_label' not in payload and 'expected_failure' not in payload
                if call.role != 'problem_designer':
                    assert 'canonical_solution' not in payload
        reviewer = s.scalar(select(db.Snapshot).where(db.Snapshot.call_id == ids[1]))
        runner = s.scalar(select(db.Snapshot).where(db.Snapshot.call_id == ids[2]))
        assert reviewer.task_input['candidate_sha256'] == runner.task_input['candidate_sha256']


def test_full_recovery_keeps_first_metrics_and_history(database):
    run_id = run_create('FULL', rounds=3, prompt='[mock:recover]')
    ids = execute_run(run_id)
    with db.transaction() as s:
        snaps = list(s.scalars(select(db.Snapshot).where(db.Snapshot.call_id == ids[6]).order_by(db.Snapshot.response_version)))
        assert len(snaps[0].ordered_history) == 2 and snaps[1].ordered_history == []
        assert snaps[1].parent_snapshot_id == snaps[0].id
        evs = list(s.scalars(select(db.Evaluation).join(db.Response).where(db.Response.call_id == ids[6]).order_by(db.Response.response_version)))
        assert evs[0].aggregate_verdict == 'violated' and evs[0].metric_snapshot['window_count'] == 3
        assert evs[1].aggregate_verdict == 'compliant' and evs[1].metric_snapshot is None
        assert s.scalar(select(func.count()).select_from(db.ToolExecution)) == 3


def test_unresolved_recovery_never_generates_third_response(database):
    ids = execute_run(run_create('FULL', prompt='[mock:violate]'))
    with db.transaction() as s:
        call = s.get(db.Call, ids[0])
        assert call.status == 'unresolved' and call.selected_response_id is None
        assert s.scalar(select(func.count()).select_from(db.Response).where(db.Response.call_id == call.id)) == 2


def test_history_includes_failures_excludes_unstarted_and_other_runs(database):
    run_id = run_create(rounds=8)
    other = run_create()
    with db.transaction() as s:
        calls = list(s.scalars(select(db.Call).where(db.Call.run_id == run_id, db.Call.role == ROLES[0]).order_by(db.Call.call_seq)))
        assert select_history(s, calls[0]) == []
        for i, call in enumerate(calls[:7]):
            call.started_at, call.finished_at = db.now(), db.now()
            call.status = 'failed' if i % 2 else 'completed'
            ensure_snapshot(s, call, 0)
        s.flush()
        assert len(select_history(s, calls[3])) == 3
        history = select_history(s, calls[7])
        assert [h['call_id'] for h in history] == [c.id for c in calls[2:7]]
        assert any(h['status']=='failed' and h['responses'] is None for h in history)
        calls[6].started_at, calls[6].status = None, 'cancelled'
        s.flush()
        assert calls[6].id not in [h['call_id'] for h in select_history(s, calls[7])]


@pytest.mark.parametrize('empty', [False, True])
def test_invalid_judge_retries_and_keeps_lexical(database, monkeypatch, empty):
    calls = []
    def bad(payload, context):
        raw = mock(payload, context)
        if context['kind'] == 'judge':
            calls.append(context)
            result = {k:{'verdict':'violated','reason':'bad evidence','evidence':'not present at all'} for k in ['INFO','SCOPE','EXEC']}
            raw['output'][0]['content'][0]['text'] = '' if empty else json.dumps(result)
        return raw
    monkeypatch.setattr(runtime, 'respond', bad)
    ids = execute_run(run_create('L1+L2', prompt='[mock:violate]'))
    assert len(calls) == 6  # Every role, including lexical pass, gets two Judge attempts.
    with db.transaction() as s:
        ev = s.scalar(select(db.Evaluation).join(db.Response).where(db.Response.call_id == ids[0]))
        assert ev.aggregate_verdict == 'violated'
        assert ev.judge_result['INFO']['verdict'] == 'uncertain'
        assert ev.evidence_validation['attempt'] == 2


def test_offline_preserves_generation_and_reuses_evaluation(database):
    run_id = run_create('Base', rounds=2)
    ids = execute_run(run_id)
    offline = offline_graph(InMemorySaver())
    with db.transaction() as s:
        before = [db.record(x) for x in s.scalars(select(db.Snapshot))]
        responses = list(s.scalars(select(db.Response).join(db.Call, db.Call.id == db.Response.call_id).where(db.Call.run_id == run_id).order_by(db.Call.call_seq)))
        before_agents = s.scalar(select(func.count()).select_from(db.ModelRequest).where(db.ModelRequest.evaluation_id.is_(None)))
    for response in responses:
        with db.transaction() as s:
            call = s.get(db.Call,response.call_id)
            snap = s.scalar(select(db.Snapshot).where(db.Snapshot.call_id == call.id))
            state={'call_id':call.id,'run_id':run_id,'role':call.role,'mode':'Base','response_id':response.id,
                   'response_version':0,'input_snapshot_id':snap.id,'phase':'offline','judge_attempt':0}
        invoke_or_resume(offline,state,'offline:'+response.id)
        invoke_or_resume(offline,state,'offline:'+response.id)
    with db.transaction() as s:
        assert before == [db.record(x) for x in s.scalars(select(db.Snapshot))]
        assert before_agents == s.scalar(select(func.count()).select_from(db.ModelRequest).where(db.ModelRequest.evaluation_id.is_(None)))
        assert s.scalar(select(func.count()).select_from(db.Evaluation).where(db.Evaluation.phase=='offline')) == 6
        assert s.scalar(select(func.count()).select_from(db.Response)) == 6


def test_checkpoint_replay_avoids_duplicate_requests(database, monkeypatch):
    from app import graphs
    run_id = run_create()
    with db.transaction() as s:
        call_id = s.scalar(select(db.Call.id).where(db.Call.run_id == run_id).order_by(db.Call.call_seq))
    original = graphs.aggregate_node
    failed = False
    def fail_after_persist(state):
        nonlocal failed
        result = original(state)
        if not failed:
            failed = True
            raise RuntimeError('simulated checkpoint boundary crash')
        return result
    monkeypatch.setattr(graphs, 'aggregate_node', fail_after_persist)
    saver = InMemorySaver()
    graph = graphs.agent_graph(saver)
    with pytest.raises(RuntimeError):
        invoke_or_resume(graph, {'call_id':call_id}, call_id)
    # Rebuild graph with the same persisted checkpoints, as a new worker would.
    graph = graphs.agent_graph(saver)
    invoke_or_resume(graph, {'call_id':call_id}, call_id)
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.Response)) == 1
        assert s.scalar(select(func.count()).select_from(db.Evaluation)) == 1
        assert s.scalar(select(func.count()).select_from(db.ModelRequest)) == 2
        assert s.get(db.Call,call_id).status == 'completed'


def test_runner_recovery_reuses_tool_and_reports_without_second_execution(database, monkeypatch):
    def false_report(payload, context):
        raw = mock(payload, context)
        if context.get('role')=='test_runner' and context.get('version')==0 and context['data'].get('tool_result'):
            # Contradict not_executed with a fabricated pass: deterministic evidence triggers L3.
            raw['output'][0]['content'][0]['text']=canonical({'status':'pass','exit_code':0,'tests':1,'failures':0,'errors':0})
        return raw
    monkeypatch.setattr(runtime,'respond',false_report)
    ids=execute_run(run_create('FULL'))
    with db.transaction() as s:
        tool=list(s.scalars(select(db.ToolExecution).where(db.ToolExecution.call_id==ids[2])))
        responses=list(s.scalars(select(db.Response).where(db.Response.call_id==ids[2]).order_by(db.Response.response_version)))
        assert len(tool)==1 and len(responses)==2
        assert json.loads(responses[1].raw_text)['status']=='not_executed'
        assert s.get(db.Call,ids[2]).selected_response_id==responses[1].id
        snap=s.scalar(select(db.Snapshot).where(db.Snapshot.call_id==ids[2],db.Snapshot.response_version==1))
        assert snap.ordered_history==[]


def test_uncertain_full_does_not_recover_and_budget_is_explicit(database):
    run_id=run_create('FULL',max_requests=1)
    with db.transaction() as s:
        call_id=s.scalar(select(db.Call.id).where(db.Call.run_id==run_id).order_by(db.Call.call_seq))
    invoke_or_resume(agent_graph(InMemorySaver()),{'call_id':call_id},call_id)
    with db.transaction() as s:
        ev=s.scalar(select(db.Evaluation).join(db.Response).where(db.Response.call_id==call_id))
        assert ev.aggregate_verdict=='uncertain' and ev.status=='error'
        assert 'budget' in ev.judge_result['INFO']['reason']
        assert s.scalar(select(func.count()).select_from(db.Response))==1
        assert s.scalar(select(func.count()).select_from(db.ModelRequest))==1
