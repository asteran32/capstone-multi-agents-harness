import hashlib

import pytest
from sqlalchemy import select, func

from app import db, dataset
from app.contracts import digest
from app.seed import seed
from test_batches import client


def test_100_original_tasks_have_100_correct_and_100_incorrect_candidates(database):
    records, provenance = dataset.load_dataset()
    assert len(records) == provenance['record_count'] == 100
    assert provenance['source_record_count'] == 164
    assert provenance['included_tasks'] == [f'HumanEval/{index}' for index in range(100)]
    assert provenance['candidate_count'] == 200
    assert provenance['candidate_generation']['incorrect_completion'] == dataset.INCORRECT_COMPLETION
    assert hashlib.sha256((dataset.DATA_DIR / 'humaneval.json').read_bytes()).hexdigest() == provenance['subset_file_sha256']
    seed()
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.Task)) == 100
        assert s.scalar(select(func.count()).select_from(db.Candidate)) == 200
        tasks = {task.source_task_id: task for task in s.scalars(select(db.Task))}
        for record in records:
            task = tasks[record['task_id']]
            assert task.source_record == record
            assert task.source_version == provenance['source_version']
            assert task.source_sha256 == digest(record)
            candidates = list(s.scalars(select(db.Candidate).where(db.Candidate.task_id == task.id)))
            assert sorted(c.private_label for c in candidates) == ['correct', 'incorrect']
            for candidate in candidates:
                completion = record['canonical_solution'] if candidate.private_label == 'correct' else dataset.INCORRECT_COMPLETION
                assert candidate.code == record['prompt'] + completion
                assert candidate.code_sha256 == digest(candidate.code)
                compile(candidate.code, record['task_id'], 'exec')


def test_config_and_catalog_expose_complete_numerically_ordered_subset(database):
    c = client()
    info = c.get('/api/config').json()['dataset']
    assert info['task_count'] == 100 and info['candidate_count'] == 200
    assert (info['first_task_id'], info['last_task_id']) == ('HumanEval/0', 'HumanEval/99')
    catalog = c.get('/api/tasks').json()
    assert [row['source_task_id'] for row in catalog] == [f'HumanEval/{index}' for index in range(100)]
    for row in catalog[30:]:
        candidates = c.get(f"/api/tasks/{row['id']}/candidates").json()
        assert [candidate['private_label'] for candidate in candidates] == ['correct', 'incorrect']


def test_corrupted_bundle_is_rejected_before_seeding(database, monkeypatch, tmp_path):
    (tmp_path / 'provenance.json').write_bytes((dataset.DATA_DIR / 'provenance.json').read_bytes())
    (tmp_path / 'humaneval.json').write_text('[]\n')
    with monkeypatch.context() as corrupted:
        corrupted.setattr(dataset, 'DATA_DIR', tmp_path)
        dataset.load_dataset.cache_clear()
        try:
            with pytest.raises(ValueError, match='SHA-256'):
                seed()
        finally:
            dataset.load_dataset.cache_clear()
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(db.Task)) == 100
        assert s.scalar(select(func.count()).select_from(db.Candidate)) == 200
