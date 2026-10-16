"""Reuse Main's invented cases against the real integrated production modules."""
import json
import time
from types import SimpleNamespace

import pytest
from futureeval_bot import forecast

def response(kind, huge=False, negative=False):
    value = (-1 if negative else 1) * 10**400 if huge else 0.4
    body = {'reasoning': 'Invented offline fixture only; no live question, reasoning or model output is used.'}
    if kind == 'binary':
        body['probability_yes'] = value
    elif kind == 'multiple_choice':
        body['probabilities'] = {'A': value, 'B': 0.6}
    else:
        body['percentiles'] = [{'percentile': p, 'value': value if huge and i == 0 else i + 1}
                               for i, p in enumerate([.1, .2, .4, .6, .8, .9])]
    return {'choices': [{'message': {'content': json.dumps(body)}}]}

class MemoryState:
    def __init__(self, responses):
        self.calls = []
        self.saved = None
        for item in responses:
            self.add(item)
    def add(self, item):
        self.calls.append({'id': len(self.calls), 'cost': 0, 'response': json.dumps(item), 'model': 'invented-fixture'})
    def completed_calls(self, qid):
        return self.calls
    def call_count(self, qid):
        return len(self.calls)
    def job(self, qid):
        return {'payload': None}
    def meta(self, name):
        return None
    def update(self, qid, **fields):
        self.saved = fields

class FakeModel:
    def __init__(self, state, kind):
        self.state, self.kind, self.calls = state, kind, 0
    def complete(self, *args, **kwargs):
        assert len(self.state.calls) < 3, 'Existing three-attempt cap exceeded'
        self.calls += 1
        self.state.add(response(self.kind))

def run(env, kind, inputs, permit_fake_calls=False):
    q = SimpleNamespace(kind=kind, question_id=1, options=['A', 'B'], scaling={'inbound_outcome_count': 200},
                        open_lower_bound=False, open_upper_bound=False)
    config = SimpleNamespace(samples_per_question=2, max_calls_per_question=3, version='offline-fixture', fingerprint='not-production')
    state = MemoryState(inputs)
    model = FakeModel(state, kind)
    result = {'reserved': len(inputs), 'fake_model_calls': 0}
    try:
        env['prepare'](state, q, config, model, {'sources': []}, time.monotonic() + (120 if permit_fake_calls else -1))
        result.update(status='prepared', payload=json.loads(state.saved['payload']),
                      valid_samples=len(json.loads(state.saved['samples'])))
    except Exception as error:
        result.update(status=type(error).__name__)
        if isinstance(error, env['NoValidatedForecast']):
            result['diagnostics'] = error.diagnostics
    result.update(reserved=len(state.calls), fake_model_calls=model.calls)
    return result

@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(forecast, 'prompt_for', lambda *args: 'Synthetic fixture only')
    # Malformed numeric values fail before SDK entry. Existing integration suite
    # separately covers real SDK conversion for ordinary numeric distributions.
    monkeypatch.setattr(forecast, 'sdk_distribution', lambda *args: [i / 200 for i in range(201)])
    return vars(forecast)

@pytest.mark.parametrize('kind', ['binary', 'multiple_choice', 'numeric', 'discrete'])
@pytest.mark.parametrize('negative', [False, True])
def test_malformed_cached_number_retains_two_valid_samples(env, kind, negative):
    good = response(kind)
    normal = run(env, kind, [good, good])
    actual = run(env, kind, [response(kind, True, negative), good, good])
    assert actual['status'] == 'prepared'
    assert actual['payload'] == normal['payload']
    assert actual['valid_samples'] == 2
    assert actual['reserved'] == 3 and actual['fake_model_calls'] == 0

@pytest.mark.parametrize('kind', ['binary', 'multiple_choice', 'numeric', 'discrete'])
def test_all_invalid_remain_rejected(env, kind):
    actual = run(env, kind, [response(kind, True)] * 3)
    assert actual['status'] == 'NoValidatedForecast'
    assert actual['reserved'] == 3 and actual['fake_model_calls'] == 0
    assert sum(actual['diagnostics']['validation_failures'].values()) == 3

@pytest.mark.parametrize('kind', ['binary', 'multiple_choice', 'numeric', 'discrete'])
def test_only_remaining_attempts_used(env, kind):
    actual = run(env, kind, [response(kind, True)], True)
    assert actual['status'] == 'prepared'
    assert actual['reserved'] == 3 and actual['fake_model_calls'] == 2

@pytest.mark.parametrize('exception', [OverflowError, RuntimeError])
def test_persistence_failure_is_outside_validation_catch(env, monkeypatch, exception):
    def fail(*args, **kwargs):
        raise exception('Synthetic persistence failure')
    monkeypatch.setattr(MemoryState, 'update', fail)
    actual = run(env, 'binary', [response('binary'), response('binary')])
    assert actual['status'] == exception.__name__
    assert actual['fake_model_calls'] == 0


def with_probabilities(probabilities):
    good = response('multiple_choice')
    data = json.loads(good['choices'][0]['message']['content'])
    data['probabilities'] = probabilities
    good['choices'][0]['message']['content'] = json.dumps(data)
    return good


@pytest.mark.parametrize('shape', [['A', 'B'], 'AB'])
def test_container_rejection_preserves_valid_cached_samples(env, shape):
    good = response('multiple_choice')
    normal = run(env, 'multiple_choice', [good, good])
    actual = run(env, 'multiple_choice', [with_probabilities(shape), good, good])
    assert actual['status'] == 'prepared'
    assert actual['payload'] == normal['payload']
    assert actual['valid_samples'] == 2 and actual['reserved'] == 3
    assert actual['fake_model_calls'] == 0


@pytest.mark.parametrize('shape', [['A', 'B'], 'AB'])
def test_all_invalid_containers_fail_closed(env, shape):
    actual = run(env, 'multiple_choice', [with_probabilities(shape)] * 3)
    assert actual['status'] == 'NoValidatedForecast'
    assert actual['diagnostics']['validation_failures'] == {'invalid_structure': 3}
    assert actual['reserved'] == 3 and actual['fake_model_calls'] == 0


@pytest.mark.parametrize('probabilities', [{'A': 0.4, 'B': 0.6}, {'A': '0.4', 'B': '0.6'}])
def test_supported_objects_remain_identical(env, probabilities):
    expected = run(env, 'multiple_choice', [response('multiple_choice')] * 2)
    actual = run(env, 'multiple_choice', [with_probabilities(probabilities)] * 2)
    assert actual == expected

