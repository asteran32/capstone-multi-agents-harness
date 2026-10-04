import json
from pathlib import Path
from sqlalchemy import select
from .contracts import digest
from .db import transaction, Task, Candidate


def seed():
    data = Path(__file__).resolve().parents[2] / 'data'
    provenance = json.loads((data / 'provenance.json').read_text())
    records = json.loads((data / 'humaneval.json').read_text())
    with transaction() as s:
        for record in records:
            if s.scalar(select(Task).where(Task.source_task_id == record['task_id'])):
                continue
            task = Task(source_task_id=record['task_id'], source_record=record,
                        source_sha256=digest(record), source_version=provenance['source_version'])
            s.add(task)
            s.flush()
            for label, completion in [('correct', record['canonical_solution']), ('incorrect', '    return None\n')]:
                code = record['prompt'] + completion
                s.add(Candidate(task_id=task.id, code=code, code_sha256=digest(code), private_label=label))


if __name__ == '__main__':
    seed()
