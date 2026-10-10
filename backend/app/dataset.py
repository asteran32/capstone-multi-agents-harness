"""The pinned bundled HumanEval subset is the source of catalog and batch sizes."""
import hashlib
import json
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / 'data'
INCORRECT_COMPLETION = '    return None\n'


@lru_cache(maxsize=1)
def load_dataset():
    metadata = json.loads((DATA_DIR / 'provenance.json').read_text())
    content = (DATA_DIR / 'humaneval.json').read_bytes()
    if hashlib.sha256(content).hexdigest() != metadata['subset_file_sha256']:
        raise ValueError('HumanEval 데이터 파일의 SHA-256이 출처 기록과 일치하지 않습니다.')
    records = json.loads(content)
    task_ids = [record['task_id'] for record in records]
    if (not records or task_ids != metadata['included_tasks'] or len(records) != metadata['record_count']
            or task_ids != [f'HumanEval/{index}' for index in range(len(records))]):
        raise ValueError('HumanEval 데이터의 문제 수·순서가 출처 기록과 일치하지 않습니다.')
    return records, metadata


def dataset_task_ids():
    return tuple(load_dataset()[1]['included_tasks'])


def dataset_info():
    ids = dataset_task_ids()
    return {'task_count': len(ids), 'candidate_count': 2 * len(ids),
            'first_task_id': ids[0], 'last_task_id': ids[-1],
            'source_version': load_dataset()[1]['source_version']}
