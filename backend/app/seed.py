from sqlalchemy import select
from .contracts import digest
from .dataset import INCORRECT_COMPLETION, load_dataset
from .db import transaction, Task, Candidate


def seed():
    records, provenance = load_dataset()
    with transaction() as s:
        for record in records:
            if s.scalar(select(Task).where(Task.source_task_id == record['task_id'])):
                continue
            task = Task(source_task_id=record['task_id'], source_record=record,
                        source_sha256=digest(record), source_version=provenance['source_version'])
            s.add(task)
            s.flush()
            for label, completion in [('correct', record['canonical_solution']), ('incorrect', INCORRECT_COMPLETION)]:
                code = record['prompt'] + completion
                s.add(Candidate(task_id=task.id, code=code, code_sha256=digest(code), private_label=label))


if __name__ == '__main__':
    seed()
