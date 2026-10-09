"""Reason constraints and frozen requests must agree with server validation."""
from copy import deepcopy
from pathlib import Path
import re

import pytest

from app.contracts import canonical, digest, evaluation_version, parse_policy
from app.evaluation_v2 import evaluation_profile
from app.evaluation_v3 import judge_payload, judge_schema, validate
from app.evaluator import is_v3


ROLES = ('problem_designer', 'code_reviewer', 'test_runner')


@pytest.mark.parametrize('role', ROLES)
@pytest.mark.parametrize('reason', ['', ' ', '\t\n', '\u00a0\u3000', '허용된 정적 검토입니다.', ' 근거 있음\n'])
@pytest.mark.parametrize('verdict,kind', [('compliant', 'none'), ('uncertain', 'unclear'),
                                         ('violated', 'unsupported_execution_claim')])
def test_reason_constraints_agree_with_server(role, reason, verdict, kind):
    schemas = judge_schema(role)['properties']['checks']['properties']
    checks = {key: dict(verdict='compliant', reason='해당 위반 근거 없음', violation_type='none',
                        evidence='', evidence_path='') for key in schemas}
    checks['execution_claim'].update(verdict=verdict, violation_type=kind, reason=reason)
    if verdict == 'violated':
        checks['execution_claim'].update(evidence='실행했습니다', evidence_path='/summary')
    execution = {'output': {'fields': {'/summary': '실행했습니다'}, 'protected_evidence': []}}
    for schema in schemas.values():
        constraint = schema['properties']['reason']
        assert 'reason' in schema['required']
        assert constraint['type'] == 'string'
        accepted = len(reason) >= constraint['minLength'] and re.search(constraint['pattern'], reason) is not None
        assert accepted == bool(reason.strip())
    _, validation = validate(canonical({'checks': checks}), role, execution)
    assert validation['valid'] == bool(reason.strip())


def test_new_profile_versions_schema_and_preserves_frozen_requests():
    policy = parse_policy(Path(__file__).resolve().parents[2].joinpath('policies/default.yaml').read_text()).model_dump()
    current = evaluation_profile(policy)
    assert current['logic'] == 'guardrail-3.2'
    for logic in ('guardrail-3.0', 'guardrail-3.1'):
        frozen = deepcopy(current)
        frozen['logic'] = logic
        for schema in frozen['judge_schemas'].values():
            for check in schema['properties']['checks']['properties'].values():
                check['properties']['reason'] = {'type': 'string'}
        config = dict(policy=policy, evaluation_profile=frozen, judge_model='mock', budget={'max_output_tokens': 1500})
        assert is_v3(config)
        before = canonical(config)
        execution = {'output': {'fields': {}, 'protected_evidence': []}}
        payload, _ = judge_payload(config, 'code_reviewer', 'id', digest('text'), 'text', {}, '', execution)
        assert payload['text']['format']['schema'] == frozen['judge_schemas']['code_reviewer']
        assert canonical(config) == before
        assert evaluation_version(policy, 'mock', 1500, current) != evaluation_version(policy, 'mock', 1500, frozen)
