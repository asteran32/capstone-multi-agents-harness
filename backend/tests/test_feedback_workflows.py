import json

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select, func

from app import db, runtime, graphs
from app.contracts import canonical, digest, evaluation_version
from app.providers import mock
from app.worker import invoke_or_resume
from test_workflows import execute_run, run_create


def test_retry_contains_failed_quote_and_preserves_target(database, monkeypatch):
    attempts = {}
    def respond(payload, context):
        result = mock(payload, context)
        if context['kind'] == 'judge':
            role = context['data']['role']
            attempts[role] = attempts.get(role, 0) + 1
            if attempts[role] == 1:
                judge = json.loads(result['output'][0]['content'][0]['text'])
                judge['INFO'].update(verdict='violated', evidence='quote from reference only')
                result['output'][0]['content'][0]['text'] = canonical(judge)
        return result
    monkeypatch.setattr(runtime, 'respond', respond)
    ids = execute_run(run_create('FULL'))
    with db.transaction() as s:
        for call_id in ids:
            requests = list(s.scalars(select(db.ModelRequest).where(db.ModelRequest.call_id == call_id,
                db.ModelRequest.evaluation_id.is_not(None)).order_by(db.ModelRequest.created_at)))
            assert len(requests) == 2
            first, second = [json.loads(r.request_payload['input'][-1]['content']) for r in requests]
            assert first['evaluation_target'] == second['evaluation_target']
            assert 'current_response' not in first and 'task_input' not in first
            assert first['reference_material'] == second['reference_material']
            feedback = second['retry_feedback']
            assert feedback['failure_kind'] == 'evidence_mismatch'
            assert feedback['failed_rules'][0]['rejected_evidence'] == 'quote from reference only'
            assert 'quote from reference only' in feedback['judge_output']
            assert 'retry_feedback' not in first
            ev = s.get(db.Evaluation, requests[0].evaluation_id)
            assert ev.evidence_validation['valid'] and len(ev.evidence_validation['attempts']) == 2
            assert s.get(db.Call, call_id).status == 'completed'
        assert s.scalar(select(func.count()).select_from(db.Response)) == 3


def test_runner_conflict_preserves_raw_judge_and_skips_l3(database, monkeypatch):
    def respond(payload, context):
        result = mock(payload, context)
        if context['kind'] == 'judge' and context['data']['role'] == 'test_runner':
            judge = json.loads(result['output'][0]['content'][0]['text'])
            for value in judge.values():
                value.update(verdict='violated', reason='Cannot prove execution inside JSON',
                             criterion_id='runner_report', evidence=context['data']['evaluation_target']['current_response'])
            result['output'][0]['content'][0]['text'] = canonical(judge)
        return result
    monkeypatch.setattr(runtime, 'respond', respond)
    ids = execute_run(run_create('FULL'))
    with db.transaction() as s:
        call = s.get(db.Call, ids[2])
        ev = s.scalar(select(db.Evaluation).join(db.Response).where(db.Response.call_id == call.id))
        assert ev.aggregate_verdict == 'compliant' and ev.judge_result['EXEC']['verdict'] == 'violated'
        assert len(ev.judgment_resolution['conflicts']) == 3
        assert ev.execution_evidence['checks']['runner_report']['verdict'] == 'compliant'
        assert s.scalar(select(func.count()).select_from(db.Response).where(db.Response.call_id == call.id)) == 1
        assert s.scalar(select(func.count()).select_from(db.ToolExecution)) == 1


def test_feedback_is_immutable_and_reused_after_checkpoint_restart(database, monkeypatch):
    run_id = run_create('FULL', prompt='[mock:recover]')
    with db.transaction() as s:
        call_id = s.scalar(select(db.Call.id).where(db.Call.run_id == run_id).order_by(db.Call.call_seq))
    original = graphs.reset_context
    crashed = False
    def fail_once(state):
        nonlocal crashed
        value = original(state)
        if not crashed:
            crashed = True
            raise RuntimeError('death after feedback snapshot persisted')
        return value
    monkeypatch.setattr(graphs, 'reset_context', fail_once)
    saver = InMemorySaver()
    import pytest
    with pytest.raises(RuntimeError):
        invoke_or_resume(graphs.agent_graph(saver), {'call_id': call_id}, call_id)
    with db.transaction() as s:
        snap = s.scalar(select(db.Snapshot).where(db.Snapshot.call_id == call_id, db.Snapshot.response_version == 1))
        before = db.record(snap)
    invoke_or_resume(graphs.agent_graph(saver), {'call_id': call_id}, call_id)
    with db.transaction() as s:
        snapshot = s.get(db.Snapshot, before['id'])
        assert db.record(snapshot) == before and snapshot.ordered_history == []
        assert snapshot.content_sha256 == digest({'role_prompt': snapshot.role_prompt, 'task_input': snapshot.task_input,
            'ordered_history': [], 'recovery_feedback': snapshot.recovery_feedback})
        assert snapshot.recovery_feedback['strategy'] == 'feedback'
        assert snapshot.recovery_feedback['confirmed_violations'][0]['source'] == 'lexical'
        request = s.scalar(select(db.ModelRequest).where(db.ModelRequest.call_id == call_id,
            db.ModelRequest.operation_key == 'online:agent:v1:step0'))
        data = json.loads(request.request_payload['input'][-1]['content'])
        assert data['recovery_feedback'] == snapshot.recovery_feedback
        assert 'only the corrected response' in request.request_payload['input'][0]['content']
        assert s.scalar(select(func.count()).select_from(db.Response).where(db.Response.call_id == call_id)) == 2
        assert s.get(db.Call, call_id).status == 'completed'


def test_legacy_run_keeps_original_judge_and_reset_protocol(database):
    run_id = run_create('FULL', prompt='[mock:recover]')
    with db.transaction() as s:
        run = s.get(db.Run, run_id)
        config = {k: v for k, v in run.config_snapshot.items() if k not in ('evaluation_profile', 'recovery_strategy')}
        config['evaluation_version'] = evaluation_version(config['policy'], config['judge_model'], config['budget']['max_output_tokens'])
        run.config_snapshot = config
    ids = execute_run(run_id)
    with db.transaction() as s:
        for ev in s.scalars(select(db.Evaluation)):
            assert ev.judgment_resolution is None
            assert ev.execution_evidence['schema_version'] == 1
        snapshots = list(s.scalars(select(db.Snapshot).where(db.Snapshot.response_version == 1)))
        assert len(snapshots) == 2
        assert all(x.recovery_feedback is None and x.reset_reason == 'confirmed_violation' for x in snapshots)
        for r in s.scalars(select(db.ModelRequest).where(db.ModelRequest.evaluation_id.is_not(None))):
            data = json.loads(r.request_payload['input'][-1]['content'])
            assert 'current_response' in data and 'evaluation_target' not in data
        assert all(s.get(db.Call, id).status == 'completed' for id in ids)
