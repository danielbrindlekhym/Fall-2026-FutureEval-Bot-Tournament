"""Synthetic error paths; no production records or network calls."""
import json
from datetime import timedelta

import pytest

from futureeval_bot.api import ServiceError
from futureeval_bot.core import Config, Contract, State, utcnow
from futureeval_bot.forecast import NoValidatedForecast, failure_category, prepare
from futureeval_bot.__main__ import run


@pytest.fixture
def setup(tmp_path):
    state = State(tmp_path / 'synthetic.db')
    q = Contract(question_id=1, post_id=2, kind='binary', title='Synthetic', background='',
        resolution='', fine_print='', close_time=(utcnow()+timedelta(days=1)).isoformat(),
        open_time=(utcnow()-timedelta(days=1)).isoformat(), project=32977)
    config = Config()
    state.discover(q, config)
    yield state, q, config
    state.close()


def test_exhaustion_reports_causes_without_response_content(setup):
    state, q, config = setup
    secret_text = 'synthetic-response-private-content-must-not-appear'
    class Model:
        sends = 0
        def complete(self, question_id, prompt, index, timeout):
            call = state.reserve_call(question_id, config.models[0], config)
            self.sends += 1
            if self.sends == 3:
                raise ServiceError('Free model transport failed; call reservation retained')
            state.finish_call(call, {'choices': [{'message': {'content': '{'+secret_text}}]}, 0)
    model = Model()
    with pytest.raises(NoValidatedForecast) as caught:
        prepare(state, q, config, model, {'sources': []}, float('inf'))
    error = caught.value
    assert error.diagnostics == {'validation_failures': {'invalid_json': 2},
        'request_failures': {'transport_unconfirmed': 1, 'call_allowance_exhausted': 1}, 'reserved_calls': 3,
        'cost_guard_halted': False}
    assert secret_text not in str(error) + json.dumps(error.diagnostics)
    assert model.sends == 3 and state.call_count(1) == 3
    assert state.job(1)['payload'] is None
    assert state.summary()['calls_without_cost_receipt'] == 1


def test_exhausted_replay_cannot_send_again(setup):
    state, q, config = setup
    for _ in range(3):
        state.reserve_call(q.question_id, config.models[0], config)
    class Model:
        sends = 0
        def complete(self, question_id, prompt, index, timeout):
            state.reserve_call(question_id, config.models[0], config)
            self.sends += 1
    model = Model()
    with pytest.raises(NoValidatedForecast) as caught:
        prepare(state, q, config, model, {'sources': []}, float('inf'))
    assert caught.value.diagnostics['request_failures'] == {'call_allowance_exhausted': 1}
    assert state.call_count(1) == 3 and model.sends == 0


@pytest.mark.parametrize('error,stage,category', [
    (ValueError('Probability outside valid bounds'), 'validation', 'probability_out_of_bounds'),
    (ValueError('Missing substantive reasoning'), 'validation', 'missing_required_reasoning'),
    (KeyError('synthetic-private-field'), 'validation', 'missing_field'),
    (TypeError('synthetic-private-body'), 'validation', 'invalid_structure'),
    (ValueError('synthetic-private-model-response'), 'validation', 'validation_rejected'),
    (ServiceError('synthetic-private-provider-body'), 'request', 'request_failed'),
])
def test_error_categories_are_fixed_and_never_echo_text(error, stage, category):
    assert failure_category(error, stage) == category


def test_run_keeps_failure_and_exports_only_safe_diagnostics(setup, monkeypatch):
    import futureeval_bot.__main__ as entry
    state, q, config = setup
    diagnostics = {'validation_failures': {'invalid_json': 2},
        'request_failures': {'transport_unconfirmed': 1}, 'reserved_calls': 3,
        'cost_guard_halted': False}
    def failed_prepare(*args):
        raise NoValidatedForecast(diagnostics)
    monkeypatch.setattr(entry, 'prepare', failed_prepare)
    monkeypatch.setattr(entry, 'research', lambda *args: {'sources': []})
    class Remote:
        def posts(self, project):
            return [{'id': 2, 'question': {'id': 1, 'type': 'binary', 'title': 'Synthetic',
                'scheduled_close_time': q.close_time, 'open_time': q.open_time, 'status': 'open'}}]
        def forecasts(self, question_id):
            return []
        def comments(self, *args, **kwargs):
            return []
        def submit_pair(self, *args):
            raise AssertionError('A failed preparation must never submit')
    class Model:
        pass
    model = Model()
    model.config = config
    result = run(config, state, Remote(), model, practice=True)
    assert result['failures'] == [{'question_id': 1, 'error_type': 'NoValidatedForecast',
        'diagnostics': diagnostics}]
    assert state.job(1)['payload'] is None
    assert model.config == config


def test_cost_halt_is_retained(setup):
    state, q, config = setup
    state.set_meta('cost_guard_halt', {'synthetic': True})
    class Model:
        def complete(self, *args, **kwargs):
            raise ServiceError('Cost guard is halted pending an audit')
    with pytest.raises(NoValidatedForecast) as caught:
        prepare(state, q, config, Model(), {'sources': []}, float('inf'))
    assert caught.value.diagnostics['cost_guard_halted'] is True
    assert state.call_count(1) == 0

