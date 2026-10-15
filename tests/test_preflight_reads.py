"""Only synthetic read-only metadata failures; no real requests or forecast content."""
from types import SimpleNamespace

import pytest
import requests

import futureeval_bot.api as api
from futureeval_bot.core import Config, State


def reply(status=200, headers=None, data=None):
    return SimpleNamespace(status_code=status, headers=headers or {}, ok=200 <= status < 300,
        json=lambda: data if data is not None else {})


def reader(items):
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        item = items[len(calls)-1]
        if isinstance(item, Exception):
            raise item
        return item
    return get, calls


@pytest.fixture
def sleeps(monkeypatch):
    values = []
    monkeypatch.setattr(api.time, 'sleep', values.append)
    return values


@pytest.mark.parametrize('failure', [requests.Timeout('private-server-text'), requests.ConnectionError('private-server-text')])
def test_one_transport_retry_can_recover_without_echoing_error(failure, sleeps):
    expected = reply()
    get, calls = reader([failure, expected])
    assert api.preflight_get(get, 'https://openrouter.ai/api/v1/key') is expected
    assert len(calls) == 2 and sleeps == [1]
    assert all(kwargs == {'timeout':25, 'allow_redirects':False} for _, kwargs in calls)


def test_repeated_transport_failure_is_bounded_and_sanitized(sleeps):
    get, calls = reader([requests.Timeout('private-a'), requests.Timeout('private-b')])
    with pytest.raises(api.ServiceError, match='after one bounded retry') as caught:
        api.preflight_get(get, 'https://openrouter.ai/api/v1/key')
    assert len(calls) == 2 and sleeps == [1]
    assert 'private' not in str(caught.value)


@pytest.mark.parametrize('status', [429, 502, 503, 504])
def test_transient_status_gets_at_most_one_retry(status, sleeps):
    last = reply(status)
    get, calls = reader([reply(status), last])
    assert api.preflight_get(get, 'https://openrouter.ai/api/v1/models') is last
    assert len(calls) == 2 and sleeps == [1.0]


@pytest.mark.parametrize('status', [200, 301, 401, 403, 400, 500])
def test_other_status_is_not_retried(status, sleeps):
    expected = reply(status)
    get, calls = reader([expected])
    assert api.preflight_get(get, 'https://openrouter.ai/api/v1/key') is expected
    assert len(calls) == 1 and sleeps == []


@pytest.mark.parametrize('delay', ['6', '60', '-1', 'NaN', 'Infinity', 'private-invalid-header'])
def test_long_or_invalid_cooldown_never_retries(delay, sleeps):
    expected = reply(429, {'Retry-After':delay})
    get, calls = reader([expected])
    assert api.preflight_get(get, 'https://openrouter.ai/api/v1/key') is expected
    assert len(calls) == 1 and sleeps == []


def test_short_cooldown_is_respected(sleeps):
    expected = reply()
    get, calls = reader([reply(429, {'Retry-After':'5'}), expected])
    assert api.preflight_get(get, 'https://openrouter.ai/api/v1/key') is expected
    assert sleeps == [5.0] and len(calls) == 2


def test_certificate_failure_never_retries(sleeps):
    get, calls = reader([requests.exceptions.SSLError('private certificate details')])
    with pytest.raises(api.ServiceError, match='certificate verification failed'):
        api.preflight_get(get, 'https://openrouter.ai/api/v1/key')
    assert len(calls) == 1 and sleeps == []


def test_preflight_recovery_uses_only_gets_and_reserves_no_model_calls(tmp_path, monkeypatch, sleeps):
    state = State(tmp_path/'synthetic.db')
    config = Config(models=['test/model:free'])
    model = api.FreeModel(config, state, 'synthetic-not-a-real-key')
    key_get, key_calls = reader([requests.Timeout(), reply(data={'data':{'free_model_daily_requests':{}}})])
    models_get, models_calls = reader([reply(503), reply(data={'data':[
        {'id':'test/model:free','pricing':{'prompt':'0','completion':'0'}}]})])
    model.session = SimpleNamespace(get=key_get)
    monkeypatch.setattr(api.requests, 'get', models_get)
    result = model.check()
    assert result['configured_free_models_available'] == ['test/model:free']
    assert len(key_calls) == len(models_calls) == 2
    assert state.summary()['reserved_model_calls'] == 0
    state.close()


def test_invalid_pricing_is_never_retried_or_accepted(tmp_path, monkeypatch, sleeps):
    state = State(tmp_path/'synthetic.db')
    model = api.FreeModel(Config(models=['test/model:free']), state, 'synthetic-not-a-real-key')
    key_get, key_calls = reader([reply(data={'data':{}})])
    models_get, models_calls = reader([reply(data={'data':[
        {'id':'test/model:free','pricing':{'prompt':'0','completion':'0.01'}}]})])
    model.session = SimpleNamespace(get=key_get)
    monkeypatch.setattr(api.requests, 'get', models_get)
    with pytest.raises(api.ServiceError, match='verified zero pricing'):
        model.check()
    assert len(key_calls) == len(models_calls) == 1 and sleeps == []
    assert state.summary()['reserved_model_calls'] == 0
    state.close()
