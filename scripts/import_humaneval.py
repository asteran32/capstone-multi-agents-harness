"""Expand the bundled subset from the exact official archive pinned in provenance.

Usage: python scripts/import_humaneval.py /path/to/HumanEval.jsonl.gz --count 100
The archive is read as data only; this command does not execute any code in it.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def import_subset(archive, count, data_dir=ROOT / 'data'):
    provenance_path = data_dir / 'provenance.json'
    dataset_path = data_dir / 'humaneval.json'
    provenance = json.loads(provenance_path.read_text())
    archive_bytes = Path(archive).read_bytes()
    if hashlib.sha256(archive_bytes).hexdigest() != provenance['source_file_sha256']:
        raise ValueError('Archive SHA-256 does not match the pinned official source.')
    source = [json.loads(line) for line in gzip.decompress(archive_bytes).splitlines() if line]
    by_id = {record['task_id']: record for record in source}
    if len(by_id) != len(source) or not 1 <= count <= len(source):
        raise ValueError('Source task IDs or requested count are invalid.')
    task_ids = [f'HumanEval/{index}' for index in range(count)]
    records = [by_id[task_id] for task_id in task_ids]
    existing = json.loads(dataset_path.read_text())
    if any(record['task_id'] not in task_ids or record != by_id.get(record['task_id']) for record in existing):
        raise ValueError('Import would remove or modify an existing task.')
    content = (json.dumps(records, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    provenance.update(included_tasks=task_ids, record_count=len(records), source_record_count=len(source),
                      subset_file_sha256=hashlib.sha256(content).hexdigest(),
                      candidate_count=2 * len(records),
                      candidate_generation={'correct': 'prompt + canonical_solution',
                                            'incorrect': 'prompt + incorrect_completion',
                                            'incorrect_completion': '    return None\n'})
    dataset_path.write_bytes(content)
    provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + '\n')
    return provenance


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--count', type=int, default=100)
    args = parser.parse_args()
    result = import_subset(args.archive, args.count)
    print(f"Imported {result['record_count']} tasks / {result['candidate_count']} candidates from {result['source_record_count']} source tasks.")
