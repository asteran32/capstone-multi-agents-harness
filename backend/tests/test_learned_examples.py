"""Exercise cumulative examples using actual persisted L3/worker outcomes."""
from copy import deepcopy
import csv
import io
import json

from langgraph.checkpoint.memory import InMemorySaver
import pytest
from sqlalchemy import select

from app import db, runtime
from app.contracts import ROLES, RunCreate, canonical, digest
from app.graphs import agent_graph, offline_graph
from app.learned_examples import MAX_EXAMPLES, MAX_ROLE_BYTES, examples_for
from app.providers import agent_payload, mock
from app.services import create_run, ensure_snapshot
from app.worker import OWNER, process_job
from test_batches import client, create as create_batch_api


def new_run(task_number=0, mode='FULL', learned=True, prompt='[mock:recover]', key=None):
    with db.transaction() as s:
        task = s.scalar(select(db.Task).where(db.Task.source_task_id == f'HumanEval/{task_number}'))
        candidate = s.scalar(select(db.Candidate).where(db.Candidate.task_id == task.id,
                                                       db.Candidate.private_label == 'correct'))
        return create_run(s, RunCreate(task_id=task.id, candidate_id=candidate.id, mode=mode,
            learned_few_shot=learned, prompts_by_role={role: prompt for role in ROLES},
            budget={'max_requests': 120}), key or db.uid()).id


def finish_run(run_id):
    with db.transaction() as s:
        job = s.scalar(select(db.Job).where(db.Job.run_id == run_id, db.Job.job_type == 'run'))
        job.status = 'running'
        job.lease_owner = OWNER
        job_id = job.id
    saver = InMemorySaver()
    process_job(job_id, agent_graph(saver), offline_graph(saver))
    with db.transaction() as s:
        assert s.get(db.Run, run_id).status == 'completed'


def bundle(run_id):
    with db.transaction() as s:
        return deepcopy(s.get(db.Run, run_id).config_snapshot['few_shot_snapshot'])


def test_verified_recovery_is_used_only_by_next_experiment_and_same_role(database):
    source = new_run()
    assert all(not values for values in bundle(source)['examples_by_role'].values())
    finish_run(source)
    target = new_run(1)
    frozen = bundle(target)
    assert [len(frozen['examples_by_role'][role]) for role in ROLES] == [1, 1, 0]
    assert frozen['sha256'] == digest({k: v for k, v in frozen.items() if k != 'sha256'})
    with db.transaction() as s:
        run = s.get(db.Run, target)
        assert run.config_snapshot['evaluation_version'] == s.get(db.Run, source).config_snapshot['evaluation_version']
        assert run.config_snapshot['policy'] == s.get(db.Run, source).config_snapshot['policy']
        for call in s.scalars(select(db.Call).where(db.Call.run_id == target)):
            snapshot = ensure_snapshot(s, call, 0)
            payload, _ = agent_payload(snapshot, call, run.config_snapshot, 0)
            assert ('<learned_agent_examples>' in payload['instructions']) == (call.role != 'test_runner')
            for role in ROLES:
                for example in frozen['examples_by_role'][role]:
                    assert (example['id'] in payload['instructions']) == (role == call.role)
                    assert example['source']['run_id'] == source
                    assert 'forbidden_literal' in example['source']['criteria']
            if call.role != 'problem_designer':
                assert 'canonical_solution' not in payload['instructions']
            assert 'private_label' not in payload['instructions']
    finish_run(target)
    with db.transaction() as s:
        calls = list(s.scalars(select(db.Call).where(db.Call.run_id == target, db.Call.role != 'test_runner')))
        for call in calls:
            snapshots = list(s.scalars(select(db.Snapshot).where(db.Snapshot.call_id == call.id)
                                       .order_by(db.Snapshot.response_version)))
            assert snapshots[0].role_prompt == snapshots[1].role_prompt
        for request in s.scalars(select(db.ModelRequest).join(db.Call).where(db.Call.run_id == target,
                                                                           db.ModelRequest.evaluation_id.is_not(None))):
            assert '<learned_agent_examples>' not in canonical(request.request_payload)
    assert bundle(target) == frozen


def test_identical_problem_excluded_and_legacy_off_base_unchanged(database):
    source = new_run()
    finish_run(source)
    for target in [new_run(), new_run(1, learned=False), new_run(1, mode='Base')]:
        with db.transaction() as s:
            call = s.scalar(select(db.Call).where(db.Call.run_id == target, db.Call.role == 'code_reviewer'))
            assert '<learned_agent_examples>' not in ensure_snapshot(s, call, 0).role_prompt
    target = new_run(1)
    with db.transaction() as s:
        run = s.get(db.Run, target)
        # Historical runs have no learned flag/bundle, even if created after candidates exist.
        run.config_snapshot = {k: v for k, v in run.config_snapshot.items() if k not in ('learned_few_shot', 'few_shot_snapshot')}
        call = s.scalar(select(db.Call).where(db.Call.run_id == target, db.Call.role == 'code_reviewer'))
        assert '<learned_agent_examples>' not in ensure_snapshot(s, call, 0).role_prompt


def test_identical_prompt_with_different_task_identity_is_excluded(database):
    source = new_run()
    finish_run(source)
    target = new_run(1)
    with db.transaction() as s:
        config = s.get(db.Run, target).config_snapshot
        example = config['few_shot_snapshot']['examples_by_role']['code_reviewer'][0]
        assert examples_for(config, 'code_reviewer', example['input']['task_input']) == []


@pytest.mark.parametrize('invalid', ['judge_error', 'uncertain', 'same_response', 'formatting_only', 'no_history_eligibility',
                                    'unfinished', 'wrong_model', 'wrong_policy', 'wrong_evaluator',
                                    'no_feedback', 'oversized'])
def test_unqualified_records_never_become_positive_examples(database, invalid):
    source = new_run()
    finish_run(source)
    with db.transaction() as s:
        run = s.get(db.Run, source)
        call = s.scalar(select(db.Call).where(db.Call.run_id == source, db.Call.role == 'code_reviewer'))
        initial = s.scalar(select(db.Response).where(db.Response.call_id == call.id, db.Response.response_version == 0))
        final = s.get(db.Response, call.selected_response_id)
        ev = s.scalar(select(db.Evaluation).where(db.Evaluation.response_id == final.id))
        snap = s.scalar(select(db.Snapshot).where(db.Snapshot.call_id == call.id, db.Snapshot.response_version == 1))
        if invalid == 'judge_error':
            first_ev = s.scalar(select(db.Evaluation).where(db.Evaluation.response_id == initial.id))
            first_ev.status, first_ev.evidence_validation = 'error', {'valid': False}
        elif invalid == 'uncertain':
            ev.aggregate_verdict = 'uncertain'
        elif invalid == 'same_response':
            final.raw_text, final.response_sha256 = initial.raw_text, initial.response_sha256
        elif invalid == 'formatting_only':
            final.raw_text = json.dumps(initial.parsed_output, indent=2)
            final.parsed_output, final.response_sha256 = initial.parsed_output, digest(final.raw_text)
        elif invalid == 'no_history_eligibility':
            ev.judgment_resolution = {**ev.judgment_resolution, 'history_eligible': False}
        elif invalid == 'unfinished':
            run.status = 'running'
        elif invalid in ('wrong_model', 'wrong_policy'):
            run.config_snapshot = {**run.config_snapshot, ('agent_model' if invalid == 'wrong_model' else 'policy_hash'): 'other'}
        elif invalid == 'wrong_evaluator':
            ev.evaluation_version = 'other'
        elif invalid == 'no_feedback':
            snap.recovery_feedback = None
        elif invalid == 'oversized':
            snap.task_input = {**snap.task_input, 'prompt': 'long source ' * 3000}
    assert not bundle(new_run(1))['examples_by_role']['code_reviewer']


def test_examples_are_bounded_and_idempotent_but_new_experiments_accumulate(database):
    for task_number in range(5):
        finish_run(new_run(task_number))
    target = new_run(7, key='frozen-target')
    frozen = bundle(target)
    for role in ROLES[:2]:
        examples = frozen['examples_by_role'][role]
        assert len(examples) == MAX_EXAMPLES
        assert len(canonical(examples).encode()) <= MAX_ROLE_BYTES
        assert [example['source']['source_task_id'] for example in examples] == ['HumanEval/4', 'HumanEval/3', 'HumanEval/2']
    finish_run(new_run(6))
    assert new_run(7, key='frozen-target') == target
    assert bundle(target) == frozen
    assert bundle(new_run(7))['sha256'] != frozen['sha256']


def test_batch_freezes_one_bundle_and_mode_rerun_does_not_adopt_new_examples(database):
    finish_run(new_run())
    c = client()
    first = create_batch_api(c, 'L1', key='frozen-batch')
    frozen = first['config_snapshot']['few_shot_snapshot']
    with db.transaction() as s:
        for item in first['items']:
            config = s.get(db.Run, item['run_id']).config_snapshot
            assert config['few_shot_snapshot'] == frozen
            call = s.scalar(select(db.Call).where(db.Call.run_id == item['run_id'], db.Call.role == 'code_reviewer'))
            prompt = ensure_snapshot(s, call, 0).role_prompt
            assert ('<learned_agent_examples>' in prompt) == (item['source_task_id'] != 'HumanEval/0')
    finish_run(new_run(1))
    exported = c.get(f"/api/batches/{first['id']}/results.csv")
    rows = list(csv.DictReader(io.StringIO(exported.content.decode('utf-8-sig'))))
    reviewer_rows = [row for row in rows if row['role'] == 'code_reviewer']
    assert all(row['few_shot_sha256'] == frozen['sha256'] for row in reviewer_rows)
    assert json.loads(reviewer_rows[0]['few_shot_example_ids']) == []
    assert json.loads(reviewer_rows[1]['few_shot_example_ids']) == [frozen['examples_by_role']['code_reviewer'][0]['id']]
    repeated = c.post(f"/api/batches/{first['id']}/rerun", json={'mode': 'FULL'}, headers={'Idempotency-Key': 'mode-rerun'})
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()['config_snapshot']['few_shot_snapshot'] == frozen
    assert create_batch_api(c, 'L1', key='frozen-batch')['id'] == first['id']
    assert create_batch_api(c, 'L1', key='new-batch')['config_snapshot']['few_shot_snapshot']['sha256'] != frozen['sha256']


def test_running_batch_results_are_not_learned_by_other_experiments(database):
    batch = create_batch_api(client(), 'FULL', prompts_by_role={role: '[mock:recover]' for role in ROLES})
    finish_run(batch['items'][0]['run_id'])
    assert all(not values for values in bundle(new_run(1))['examples_by_role'].values())
    with db.transaction() as s:
        for item in batch['items'][1:]:
            job = s.scalar(select(db.Job).where(db.Job.run_id == item['run_id']))
            job.status = 'cancelled'
    assert bundle(new_run(1))['examples_by_role']['code_reviewer']


def test_runner_examples_include_only_matching_tool_evidence(database, monkeypatch):
    def respond(payload, context):
        result = mock(payload, context)
        if (context['kind'] == 'agent' and context['role'] == 'test_runner' and context['version'] == 0
                and result['output'][0]['type'] == 'message'):
            result['output'][0]['content'][0]['text'] = canonical(
                {'status': 'pass', 'exit_code': 0, 'tests': 1, 'failures': 0, 'errors': 0})
        return result
    monkeypatch.setattr(runtime, 'respond', respond)
    source = new_run()
    finish_run(source)
    examples = bundle(new_run(1))['examples_by_role']['test_runner']
    assert len(examples) == 1
    example = examples[0]
    assert json.loads(example['output']) == example['input']['tool_result']
    assert set(example['input']['tool_result']) == {'status', 'exit_code', 'tests', 'failures', 'errors'}
    assert 'canonical_solution' not in canonical(example)
