import httpx
import pytest

from app import providers
from app.config import settings


def test_provider_bad_request_includes_message_and_parameter(monkeypatch):
    monkeypatch.setitem(settings.__dict__, 'openai_key', 'test-provider-key')
    raw = {'error': {
        'type': 'invalid_request_error', 'param': 'input', 'code': None,
        'message': "Response input messages must contain the word 'json' in some form to use 'text.format' of type 'json_object'."}}
    requests = []

    def post(url, **kwargs):
        requests.append(kwargs)
        return httpx.Response(400, json=raw)

    monkeypatch.setattr(providers.httpx, 'post', post)
    with pytest.raises(providers.ProviderError) as caught:
        providers.respond({'model': 'gpt-5.4-nano', 'input': 'Evaluate this response'}, {})
    error = caught.value
    assert str(error) == f"Provider HTTP 400: {raw['error']['message']} (param: input)"
    assert error.raw == raw
    assert not error.retryable
    assert len(requests) == 1
    assert settings.openai_key not in str(error)


def test_provider_non_json_error_remains_retryable(monkeypatch):
    monkeypatch.setitem(settings.__dict__, 'openai_key', 'test-provider-key')
    monkeypatch.setattr(providers.httpx, 'post', lambda *args, **kwargs: httpx.Response(502, text='Bad Gateway'))
    with pytest.raises(providers.ProviderError) as caught:
        providers.respond({'model': 'gpt-5.4-nano'}, {})
    assert str(caught.value) == 'Provider HTTP 502'
    assert caught.value.raw == {'raw_text': 'Bad Gateway'}
    assert caught.value.retryable
