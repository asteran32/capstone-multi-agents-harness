"""Runs only inside the network-disabled disposable image. Never run on the host."""
import base64
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET


def limits():
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (8, 9))


def test_program(payload):
    # HumanEval tests share the candidate's globals, including prompt helpers.
    # Keep both source strings unchanged, as in the upstream evaluation harness.
    return (payload['code'] + '\n' + payload['test'] +
            '\n\ndef test_original_humaneval():\n    check(' + payload['entry_point'] + ')\n')


def main():
    payload = json.loads(base64.b64decode(sys.argv[1]))
    work = Path('/tmp/work')
    work.mkdir()
    (work / 'candidate.py').write_text(payload['code'])
    (work / 'test_original.py').write_text(test_program(payload))
    result = {'status': 'error', 'exit_code': None, 'tests': None, 'failures': None, 'errors': None}
    with (work / 'stdout').open('wb') as stdout, (work / 'stderr').open('wb') as stderr:
        process = subprocess.Popen([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                                    '--junitxml=result.xml', 'test_original.py::test_original_humaneval'], cwd=work,
                                   stdout=stdout, stderr=stderr, start_new_session=True, preexec_fn=limits,
                                   env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1',
                                        'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'})
        try:
            result['exit_code'] = process.wait(timeout=10)
            result['status'] = 'pass' if process.returncode == 0 else ('fail' if process.returncode == 1 else 'error')
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            result.update(status='timeout', exit_code=process.returncode)
    xml = (work / 'result.xml').read_text() if (work / 'result.xml').exists() else ''
    if xml:
        try:
            suites = ET.fromstring(xml).iter('testsuite')
            counts = {'tests': 0, 'failures': 0, 'errors': 0}
            for suite in suites:
                for key in counts:
                    counts[key] += int(suite.attrib.get(key, 0))
            result.update(counts)
        except (ValueError, ET.ParseError):
            result.update(status='error', reason='Invalid junit XML')
    print(json.dumps({'state': 'completed', 'result': result,
                      'stdout': (work / 'stdout').read_text(errors='replace'),
                      'stderr': (work / 'stderr').read_text(errors='replace'), 'junit_xml': xml}))


if __name__ == '__main__':
    main()
