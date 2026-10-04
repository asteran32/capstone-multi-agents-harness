"""Run source-preserved correct/incorrect candidates via the configured isolated executor."""
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4
import httpx

root=Path(__file__).resolve().parents[1]
task=json.loads((root/'data/humaneval.json').read_text())[0]
client=httpx.Client(base_url=os.getenv('SANDBOX_URL','http://sandbox:8090'),
                   headers={'Authorization':'Bearer '+os.environ['SANDBOX_TOKEN']},timeout=40)
for label,completion,expected in [('correct',task['canonical_solution'],'pass'),('incorrect','    return None\n','fail')]:
    code=task['prompt']+completion
    payload={'code':code,'test':task['test'],'entry_point':task['entry_point'],
             'code_sha256':hashlib.sha256(code.encode()).hexdigest(),'test_sha256':hashlib.sha256(task['test'].encode()).hexdigest()}
    operation=str(uuid4())
    response=client.post('/executions/'+operation,json=payload)
    response.raise_for_status()
    result=response.json()
    assert result['result']['status']==expected,result
    repeated=client.post('/executions/'+operation,json=payload)
    assert repeated.json()==result,'Result was not reused'
    assert result['junit_xml'] and result['result']['tests']==1
    print(label,expected,'and idempotent replay verified')
