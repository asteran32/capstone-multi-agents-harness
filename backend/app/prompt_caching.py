"""Versioned wire layout for new batch experiments; no response-result cache.

Keep a single JSON object in the original user message. Its stable fields form
the first text block; task-specific fields finish the same object in the next
block. Only the reusable prefix is written to OpenAI's prompt cache.
"""
from copy import deepcopy
import json
import re

from .contracts import canonical

BATCH_PROMPT_CACHE = {'version': 'fixed-prefix-v1', 'mode': 'explicit', 'ttl': '30m'}


def supports_explicit_caching(model):
    # The explicit API is supported by GPT-5.6 and later, not earlier GPT-5
    # models, o-series models, mock, or aliases with unknown capabilities.
    match = re.match(r'^gpt-(\d+)(?:\.(\d+))?(?:-|$)', model or '')
    return bool(match and (int(match[1]), int(match[2] or 0)) >= (5, 6))


def _split_json(data, kind):
    keys = ('role', 'user_prompt') if kind == 'agent' else (
        'role', 'policy', 'criteria', 'demonstrations', 'user_prompt')
    fixed = {key: data[key] for key in keys if key in data}
    variable = {key: value for key, value in data.items() if key not in fixed}
    if not fixed or not variable:
        return None
    prefix = canonical(fixed)[:-1]
    reference = variable.get('reference_material') if kind == 'judge' else None
    if isinstance(reference, dict) and 'user_prompt' in reference:
        # Preserve reference_material.user_prompt and .task_input at their
        # original paths without duplicating the reference_material key.
        variable.pop('reference_material')
        prefix += ',"reference_material":' + canonical({'user_prompt': reference['user_prompt']})[:-1]
        remaining = {key: value for key, value in reference.items() if key != 'user_prompt'}
        if remaining:
            prefix += ','
            suffix = canonical(remaining)[1:]
            if variable:
                suffix += ',' + canonical(variable)[1:-1]
            return prefix, suffix + '}'
        prefix += '}'
    if not variable:
        return None
    return prefix + ',', canonical(variable)[1:]


def batch_cached_payload(payload, config, kind):
    """Return the exact payload to reserve, persist and send, without mutation.

The policy is frozen only on newly created batches. Live, standalone runs,
historical batches and unsupported models retain their original wire format.
"""
    if (not config.get('batch_id') or config.get('batch_prompt_cache') != BATCH_PROMPT_CACHE
            or config.get('live_session_id') or config.get('live_stage')
            or kind not in ('agent', 'judge')
            or not supports_explicit_caching(payload.get('model'))
            or 'prompt_cache_options' in payload):
        return payload
    inputs = payload.get('input')
    if not isinstance(inputs, list):
        return payload
    users = [index for index, message in enumerate(inputs) if message.get('role') == 'user']
    if len(users) != 1 or not isinstance(inputs[users[0]].get('content'), str):
        return payload
    try:
        data = json.loads(inputs[users[0]]['content'])
    except (ValueError, TypeError):
        return payload
    if not isinstance(data, dict):
        return payload
    parts = _split_json(data, kind)
    if parts is None:
        return payload
    prefix, suffix = parts
    result = deepcopy(payload)
    result['input'][users[0]]['content'] = [
        {'type': 'input_text', 'text': prefix, 'prompt_cache_breakpoint': {'mode': 'explicit'}},
        {'type': 'input_text', 'text': suffix},
    ]
    result['prompt_cache_options'] = {key: BATCH_PROMPT_CACHE[key] for key in ('mode', 'ttl')}
    # Cache metadata must not make an otherwise valid near-limit request fail.
    limit = config.get('budget', {}).get('max_input_bytes')
    if limit is not None and len(canonical(result).encode()) > limit:
        return payload
    return result
