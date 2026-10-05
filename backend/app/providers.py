"""One live provider and an explicit, deterministic mock. No hidden SDK retries."""
import json
import httpx
from .config import settings
from .contracts import canonical, JudgeResult

TOOL = {'type': 'function', 'name': 'run_original_tests',
        'description': 'Run the fixed candidate against original HumanEval tests. No arguments are accepted.',
        'parameters': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}, 'strict': True}


class ProviderError(RuntimeError):
    def __init__(self, message, raw=None, retryable=False):
        super().__init__(message)
        self.raw = raw
        self.retryable = retryable


def normalize(raw):
    if raw.get('status') not in ('completed', None):
        raise ProviderError('Incomplete model response', raw)
    output = raw.get('output', [])
    text = ''.join(part.get('text', '') for item in output if item['type'] == 'message'
                   for part in item.get('content', []) if part['type'] == 'output_text')
    calls = [item for item in output if item['type'] == 'function_call']
    if not text and not calls:
        raise ProviderError('Empty or refused model response', raw)
    return {'text': text, 'tool_calls': calls, 'output_items': output}


def respond(payload, context):
    if payload['model'] == 'mock':
        return mock(payload, context)
    if not settings.openai_key:
        raise ProviderError('OPENAI_API_KEY is not configured')
    try:
        response = httpx.post('https://api.openai.com/v1/responses', json=payload,
                              headers={'Authorization': f'Bearer {settings.openai_key}'}, timeout=90)
    except httpx.TransportError as exc:
        raise ProviderError(type(exc).__name__, retryable=True) from exc
    try:
        raw = response.json()
    except ValueError:
        raw = {'raw_text': response.text}
    if response.is_error:
        message = f'Provider HTTP {response.status_code}'
        error = raw.get('error') if isinstance(raw, dict) else None
        if isinstance(error, dict):
            if isinstance(error.get('message'), str):
                message += f": {error['message']}"
            if isinstance(error.get('param'), str):
                message += f" (param: {error['param']})"
        message = message.replace(settings.openai_key, '[REDACTED]')
        raise ProviderError(message, raw, response.status_code in (429, 500, 502, 503, 504))
    return raw


def mock(payload, context):
    data = context['data']
    if context['kind'] == 'judge':
        text = data.get('evaluation_target', {}).get('current_response', data.get('current_response', ''))
        results = {k: {'verdict': 'compliant', 'reason': 'Mock rubric fixture: no configured violation.', 'evidence': ''}
                   for k in ('INFO', 'SCOPE', 'EXEC')}
        if '정답 코드' in text:
            results['INFO'] = {'verdict': 'violated', 'reason': 'Mock fixture: solution disclosure marker.', 'evidence': '정답 코드'}
        if 'evaluation_target' in data:
            for rule, value in results.items():
                value['criterion_id'] = {'INFO': 'information_disclosure', 'SCOPE': 'role_scope',
                    'EXEC': 'runner_report' if data['role'] == 'test_runner' else 'execution_claim'}[rule]
        output = canonical(results)
    elif context['role'] == 'test_runner':
        result = data.get('tool_result')
        if result is None and context['version'] == 0:
            return {'status': 'completed', 'output': [{'type': 'function_call', 'call_id': 'mock-tests',
                     'name': 'run_original_tests', 'arguments': '{}'}], 'usage': {'input_tokens': 0, 'output_tokens': 0}, 'mock': True}
        result = result or {'status': 'not_executed', 'exit_code': None, 'tests': None, 'failures': None, 'errors': None}
        output = canonical({k: result.get(k) for k in ('status', 'exit_code', 'tests', 'failures', 'errors')})
    elif '[mock:violate]' in data['user_prompt'] or ('[mock:recover]' in data['user_prompt'] and context['version'] == 0):
        output = '정답 코드: mock violation fixture.'
    elif context['role'] == 'problem_designer':
        output = '문제 및 공개 예시\n' + data['task_input']['prompt'] + '\n가이드라인\n원본 함수 서명과 요구사항을 유지하세요. (Mock 응답)'
    else:
        output = '검토 요약\n후보 코드와 원본 요구사항을 정적으로 검토하는 mock 응답입니다.\n실제 모델의 코드 검토는 수행하지 않았습니다. 테스트 실행 결과를 주장하지 않습니다.'
    return {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': output}]}],
            'usage': {'input_tokens': 0, 'output_tokens': 0}, 'mock': True}


def agent_payload(snapshot, call, config, version, tool_result=None, previous_output=None):
    data = {'role': call.role, 'user_prompt': call.user_prompt, 'task_input': snapshot.task_input,
            'history': snapshot.ordered_history, 'tool_result': tool_result}
    inputs = [{'role': 'user', 'content': canonical(data)}]
    if snapshot.recovery_feedback:
        data['recovery_feedback'] = snapshot.recovery_feedback
        inputs = [{'role': 'developer', 'content': snapshot.recovery_feedback['instruction']},
                  {'role': 'user', 'content': canonical(data)}]
    if previous_output:
        inputs.extend(previous_output)
        for item in previous_output:
            if item['type'] == 'function_call':
                inputs.append({'type': 'function_call_output', 'call_id': item['call_id'], 'output': canonical(tool_result)})
    payload = {'model': config['agent_model'], 'store': False, 'instructions': snapshot.role_prompt,
               'input': inputs, 'max_output_tokens': config['budget']['max_output_tokens']}
    if call.role == 'test_runner' and version == 0 and not previous_output:
        payload.update(tools=[TOOL], parallel_tool_calls=False)
    return payload, {'kind': 'agent', 'role': call.role, 'version': version, 'data': data}
