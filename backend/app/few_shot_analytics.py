"""Read-only counts of persisted requests using cumulative recovery examples."""
from collections import Counter, defaultdict

from sqlalchemy import select

from .contracts import ROLES, canonical
from .db import Snapshot
from .learned_examples import examples_for


def _counts(observations):
    return {
        'applications': len(observations),
        'initial': sum(item['response_version'] == 0 for item in observations),
        'recovery': sum(item['response_version'] == 1 for item in observations),
        'unique_calls': len({item['call_id'] for item in observations}),
        'unique_examples': len({example_id for item in observations for example_id in item['example_ids']}),
    }


def few_shot_analytics(s, batch, runs, calls, requests):
    # A saved prompt alone is not an application: budgets can stop generation
    # before a model request is reserved. Retries and tool steps share a snapshot.
    by_snapshot = defaultdict(list)
    for request in requests:
        if request.evaluation_id is not None or not request.operation_key.startswith('online:agent:'):
            continue
        call = calls[request.call_id]
        config = runs[call.run_id].config_snapshot
        selected = config.get('few_shot_snapshot', {}).get('examples_by_role', {}).get(call.role)
        if config.get('learned_few_shot') and config.get('mode') != 'Base' and selected:
            by_snapshot[request.input_snapshot_id].append(request)

    observations = []
    if by_snapshot:
        query = select(Snapshot.id, Snapshot.call_id, Snapshot.response_version, Snapshot.role_prompt,
                       Snapshot.task_input['prompt'].as_string().label('task_prompt')).where(
            Snapshot.id.in_(by_snapshot), Snapshot.call_id.in_(calls), Snapshot.response_version.in_([0, 1]))
        for snapshot in s.execute(query).yield_per(100):
            call = calls[snapshot.call_id]
            submitted = [request for request in by_snapshot[snapshot.id]
                         if request.call_id == call.id and request.operation_key.startswith(
                             f'online:agent:v{snapshot.response_version}:')]
            if not submitted:
                continue
            config = runs[call.run_id].config_snapshot
            examples = examples_for(config, call.role, {'prompt': snapshot.task_prompt})
            if not examples:
                continue
            demonstrations = [{key: example[key] for key in ('id', 'input', 'output', 'boundary_note')}
                              for example in examples]
            # Verify the actual saved structured block, independent of surrounding
            # policy prose. Merely selecting examples or mentioning an ID is not use.
            block = '\n<learned_agent_examples>\n' + canonical(demonstrations) + '\n</learned_agent_examples>'
            if block not in snapshot.role_prompt:
                continue
            observations.append({
                'call_id': call.id, 'role': call.role, 'response_version': snapshot.response_version,
                'input_snapshot_id': snapshot.id, 'example_ids': [example['id'] for example in examples],
                'examples': [{'id': example['id'], **{key: example['source'][key] for key in
                              ('run_id', 'call_id', 'source_task_id', 'criteria')}} for example in examples],
                'request_count': len(submitted), 'request_states': dict(Counter(r.status for r in submitted)),
            })

    config = batch.config_snapshot
    return {
        'recorded': bool(config.get('few_shot_snapshot')),
        'enabled': bool(config.get('learned_few_shot') and config.get('mode') != 'Base'),
        **_counts(observations),
        'roles': [{'role': role, **_counts([item for item in observations if item['role'] == role])}
                  for role in ROLES],
    }, observations
