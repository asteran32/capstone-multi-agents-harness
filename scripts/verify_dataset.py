"""Check every bundled candidate through /runner.py inside the sandbox image only.

Mount data at /data and this file at /verify_dataset.py in a disposable, networkless
guardrail-sandbox:local container. Never evaluate candidates in the host process.
"""
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    if not Path('/.dockerenv').is_file() or not Path('/runner.py').is_file():
        raise SystemExit('Run this verifier inside the disposable sandbox container.')
    records = json.loads(Path('/data/humaneval.json').read_text())
    provenance = json.loads(Path('/data/provenance.json').read_text())
    incorrect = provenance['candidate_generation']['incorrect_completion']
    counts, failures = {'correct': 0, 'incorrect': 0}, []
    for index, record in enumerate(records, 1):
        for label, completion, expected in [('correct', record['canonical_solution'], 'pass'),
                                             ('incorrect', incorrect, 'fail')]:
            code = record['prompt'] + completion
            payload = {'code': code, 'test': record['test'], 'entry_point': record['entry_point'],
                       'code_sha256': hashlib.sha256(code.encode()).hexdigest(),
                       'test_sha256': hashlib.sha256(record['test'].encode()).hexdigest()}
            encoded = base64.b64encode(json.dumps(payload).encode()).decode()
            try:
                child = subprocess.run([sys.executable, '/runner.py', encoded], capture_output=True, text=True, timeout=20)
                result = json.loads(child.stdout)
                if child.returncode or result['result']['status'] != expected:
                    failures.append({'task_id': record['task_id'], 'label': label, 'expected': expected, **result})
                else:
                    counts[label] += 1
            except (subprocess.TimeoutExpired, ValueError) as exc:
                failures.append({'task_id': record['task_id'], 'label': label, 'error': str(exc)})
            finally:
                # /runner.py owns this directory inside the disposable /tmp mount.
                shutil.rmtree('/tmp/work', ignore_errors=True)
        if index % 10 == 0:
            print(f"Verified {index}/{len(records)} tasks: {counts}", flush=True)
    print(json.dumps({'tasks': len(records), 'verified_candidates': counts, 'failures': failures}, ensure_ascii=False), flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
