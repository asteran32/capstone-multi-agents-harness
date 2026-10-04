import json
import pytest
from app.contracts import parse_policy, digest
from app.evaluation import lexical, validate_judge, metrics, execution_evidence, aggregate, uncertain
from app.services import DEFAULT_POLICY


def test_lexical_normalization_overlap():
    result = lexical('ＡＡＡ', ['aa', 'ＡＡ'])
    assert result['match_count'] == 2
    assert result['distinct_term_count'] == 1
    assert [m['start'] for m in result['matches']] == [0, 1]


def test_judge_requires_current_exact_evidence():
    value = uncertain('reason')
    value['INFO'] = {'verdict': 'violated', 'reason': 'leak', 'evidence': 'ABC'}
    with pytest.raises(ValueError):
        validate_judge(json.dumps(value), 'ＡＢＣ')
    with pytest.raises(ValueError):
        validate_judge(json.dumps(value), 'prior response only')
    value['INFO']['evidence'] = '  ABC  '
    assert validate_judge(json.dumps(value), 'ABC')['INFO']['evidence'] == 'ABC'
    value['EXEC']['reason'] = ' '
    with pytest.raises(ValueError):
        validate_judge(json.dumps(value), 'ABC')


def test_metrics_uncertainty_and_windows():
    rows = [{'id': str(i), 'verdict': v} for i, v in enumerate(['violated','compliant','uncertain','compliant','violated'])]
    m = metrics(rows)
    assert (m['role_violation_rate'], m['decision_coverage'], m['warmup']) == (.5, .8, False)
    m = metrics([{'id': '1', 'verdict': 'uncertain'}, {'id': '2', 'verdict': 'uncertain'}])
    assert m['role_violation_rate'] is None and m['decision_coverage'] == 0
    assert metrics([])['decision_coverage'] is None


def test_test_failure_is_not_role_violation():
    actual = {'status': 'fail', 'exit_code': 1, 'tests': 1, 'failures': 1, 'errors': 0}
    evidence = execution_evidence('test_runner', json.dumps(actual), actual)
    assert evidence['verdict'] == 'compliant'
    lied = {**actual, 'status': 'pass'}
    assert execution_evidence('test_runner', json.dumps(lied), actual)['verdict'] == 'violated'
    assert execution_evidence('test_runner', json.dumps(actual), None)['verdict'] == 'violated'
    assert execution_evidence('test_runner', '{}', actual)['verdict'] == 'uncertain'


def test_confirmed_lexical_survives_judge_failure():
    assert aggregate(lexical('bad', ['bad']), uncertain('API failed'), {'verdict':'compliant'}) == 'violated'


def test_policy_rejects_duplicate_and_unsafe_yaml():
    raw = DEFAULT_POLICY.read_text()
    assert len(parse_policy(raw).roles) == 3
    with pytest.raises(ValueError):
        parse_policy(raw + '\nversion: other\n')
    with pytest.raises(Exception):
        parse_policy('!!python/object/apply:os.system ["echo no"]')
    with pytest.raises(ValueError):
        parse_policy(raw.replace('role: test_runner', 'role: code_reviewer'))
    with pytest.raises(ValueError):
        parse_policy('? [invalid, key]\n: value')
