import json
from copy import deepcopy

import pytest
import yaml
from sqlalchemy import select

from app import db, evaluation_v2
from app.batches import create_batch
from app.contracts import BatchCreate, ROLES, evaluation_version
from test_workflows import run_create


@pytest.fixture()
def judge_file(tmp_path, monkeypatch):
    document = evaluation_v2.load_judge_prompts()
    path = tmp_path / 'judge.yaml'
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding='utf-8')
    monkeypatch.setattr(evaluation_v2, 'JUDGE_POLICY', path)
    return path, document


def test_yaml_changes_apply_to_new_runs_but_requests_use_frozen_prompts(database, judge_file):
    path, document = judge_file
    first_id = run_create()
    document['common_rubric'] = '새 공통 판정 기준\nKeep this newline.\n'
    document['role_rubrics'] = {role: f'New rubric for {role}' for role in ROLES}
    document['retry_instruction'] = 'New validation retry instruction'
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding='utf-8')
    second_id = run_create()
    with db.transaction() as s:
        first = deepcopy(s.get(db.Run, first_id).config_snapshot)
        second = deepcopy(s.get(db.Run, second_id).config_snapshot)
    assert first['evaluation_version'] != second['evaluation_version']
    assert second['evaluation_profile']['common_rubric'] == document['common_rubric']
    # A missing/changed file must not affect a previously created run or its retries.
    path.unlink()
    for config in (first, second):
        profile = config['evaluation_profile']
        assert config['evaluation_version'] == evaluation_version(
            config['policy'], config['judge_model'], config['budget']['max_output_tokens'], profile)
        for role in ROLES:
            payload, data = evaluation_v2.judge_payload(
                config, role, 'response-id', 'sha256', 'fixed response', {}, 'request', {},
                {'failure_kind': 'schema', 'error': 'fixture'})
            assert payload['instructions'] == profile['common_rubric'] + '\n' + profile['role_rubrics'][role]
            assert data['retry_feedback']['instruction'] == profile['retry_instruction']
            assert json.loads(payload['input'][-1]['content']) == data


def test_batch_freezes_one_file_revision_for_all_thirty_runs(database, judge_file, monkeypatch):
    path, document = judge_file
    original = evaluation_v2.load_judge_prompts
    reads = []

    def changing_file():
        prompts = original()
        reads.append(prompts)
        changed = {**document, 'common_rubric': 'File edited during batch creation'}
        path.write_text(yaml.safe_dump(changed), encoding='utf-8')
        return prompts

    monkeypatch.setattr(evaluation_v2, 'load_judge_prompts', changing_file)
    with db.transaction() as s:
        batch = create_batch(s, BatchCreate(prompts_by_role={r: 'request' for r in ROLES}), 'yaml-batch')
        runs = list(s.scalars(select(db.Run).where(db.Run.id.in_([i['run_id'] for i in batch.manifest]))))
        assert len(runs) == 30 and len(reads) == 1
        assert len({r.config_snapshot['evaluation_version'] for r in runs}) == 1
        assert all(r.config_snapshot['evaluation_profile']['common_rubric'] == document['common_rubric'] for r in runs)


@pytest.mark.parametrize('field,value', [
    ('common_rubric', ' \n\t'),
    ('retry_instruction', None),
    ('common_rubric', 123),
    ('role_rubrics', {'problem_designer': 'Missing the other roles'}),
    ('role_rubrics', {**{r: 'rubric' for r in ROLES}, 'unknown_role': 'rubric'}),
    ('role_rubrics', {**{r: 'rubric' for r in ROLES}, 'test_runner': ''}),
    ('unknown_field', 'typo'),
])
def test_invalid_prompt_definitions_fail_explicitly(judge_file, field, value):
    path, document = judge_file
    document[field] = value
    path.write_text(yaml.safe_dump(document), encoding='utf-8')
    with pytest.raises(ValueError, match='Judge 프롬프트 파일'):
        evaluation_v2.evaluation_profile()


@pytest.mark.parametrize('raw', [None, '', 'common_rubric: [', 'common_rubric: first\ncommon_rubric: second\n'])
def test_missing_malformed_and_duplicate_yaml_fail_without_fallback(judge_file, raw):
    path, _ = judge_file
    if raw is None:
        path.unlink()
    else:
        path.write_text(raw, encoding='utf-8')
    with pytest.raises(ValueError, match='Judge 프롬프트 파일'):
        evaluation_v2.evaluation_profile()
