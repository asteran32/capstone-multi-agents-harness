"""Private executor service. Only this process has Docker socket access."""
import base64
import fcntl
import hashlib
import json
import os
import secrets
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import docker
from docker.errors import NotFound
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
JOURNAL = Path(os.getenv('SANDBOX_JOURNAL', '/journal'))
IMAGE = os.getenv('SANDBOX_IMAGE', 'guardrail-sandbox:local')
TOKEN = os.getenv('SANDBOX_TOKEN', '')
JOURNAL.mkdir(parents=True, exist_ok=True)


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str = Field(max_length=100000)
    test: str = Field(max_length=100000)
    entry_point: str = Field(pattern=r'^[A-Za-z_][A-Za-z_0-9]*$')
    code_sha256: str
    test_sha256: str

    @model_validator(mode='after')
    def hashes_match(self):
        for key in ('code', 'test'):
            if hashlib.sha256(getattr(self, key).encode()).hexdigest() != getattr(self, key + '_sha256'):
                raise ValueError('Code/test hash mismatch')
        return self


def authenticate(authorization: str = Header(default='')):
    if not TOKEN or not secrets.compare_digest(authorization, f'Bearer {TOKEN}'):
        raise HTTPException(401)


def save(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w') as f:
        json.dump(value, f)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


@contextmanager
def operation(id):
    root = JOURNAL / str(id)
    root.mkdir(exist_ok=True)
    with (root / 'lock').open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield root


def collect(client, name, root):
    result_file = root / 'result.json'
    if result_file.exists():
        return json.loads(result_file.read_text())
    try:
        container = client.containers.get(name)
    except NotFound:
        return {'state': 'unknown', 'reason': 'Launch journal exists but container is missing. No automatic re-execution.'}
    container.reload()
    if container.status == 'created':
        # Starting a container in CREATED state is safe; it has never executed.
        container.start()
    try:
        exit_status = container.wait(timeout=22)['StatusCode']
    except Exception:
        container.reload()
        if container.status == 'running':
            container.kill()
            container.wait(timeout=5)
        exit_status = 124
    stdout = container.logs(stdout=True, stderr=False).decode(errors='replace')
    stderr = container.logs(stdout=False, stderr=True).decode(errors='replace')
    try:
        envelope = json.loads(stdout)
        if envelope.get('state') != 'completed' or exit_status != 0:
            raise ValueError('Sandbox wrapper did not complete')
    except (ValueError, TypeError):
        envelope = {'state': 'completed', 'result': {'status': 'timeout' if exit_status in (124, 137) else 'error',
                    'exit_code': exit_status, 'tests': None, 'failures': None, 'errors': None},
                    'stdout': stdout, 'stderr': stderr, 'junit_xml': ''}
    envelope['result'].update(code_sha256=json.loads((root / 'request.json').read_text())['code_sha256'],
                              test_sha256=json.loads((root / 'request.json').read_text())['test_sha256'],
                              sandbox='docker', image_id=container.image.id)
    save(result_file, envelope)
    container.remove(force=True)
    return envelope


@app.post('/executions/{id}', dependencies=[Depends(authenticate)])
def execute(id: UUID, body: Input):
    with operation(id) as root:
        client = docker.from_env(timeout=30)
        name = f'guardrail-test-{id}'
        payload = body.model_dump()
        request_file = root / 'request.json'
        if request_file.exists():
            if json.loads(request_file.read_text()) != payload:
                raise HTTPException(409, 'operation key payload mismatch')
            return collect(client, name, root)
        # Durable launch intent precedes Docker create. An ambiguous crash is never retried blindly.
        save(request_file, payload)
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        try:
            client.containers.create(IMAGE, ['timeout', '-k', '1', '15', 'python', '/runner.py', encoded],
                name=name, network_mode='none', user='65534:65534', read_only=True,
                cap_drop=['ALL'], security_opt=['no-new-privileges'], mem_limit='256m', memswap_limit='256m',
                nano_cpus=1_000_000_000, pids_limit=64, tmpfs={'/tmp': 'rw,noexec,nosuid,size=32m,mode=1777'},
                environment={'PYTHONDONTWRITEBYTECODE': '1'}, use_config_proxy=False,
                log_config=docker.types.LogConfig(type='json-file', config={'max-size': '8m', 'max-file': '1'}))
        except Exception as exc:
            # Before successful create, no test ran. Retain a terminal operational error.
            envelope = {'state': 'completed', 'result': {'status': 'not_executed', 'exit_code': None,
                        'tests': None, 'failures': None, 'errors': None, 'reason': type(exc).__name__},
                        'stdout': '', 'stderr': str(exc), 'junit_xml': ''}
            save(root / 'result.json', envelope)
            return envelope
        return collect(client, name, root)


@app.get('/executions/{id}', dependencies=[Depends(authenticate)])
def inspect(id: UUID):
    with operation(id) as root:
        if not (root / 'request.json').exists():
            raise HTTPException(404)
        return collect(docker.from_env(timeout=30), f'guardrail-test-{id}', root)
