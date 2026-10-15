"""Synthetic requests and temporary SQLite/checkpoints only; never live data."""
import json
import sqlite3
from types import SimpleNamespace

import pytest
import requests

from futureeval_bot.api import FreeModel, ServiceError
from futureeval_bot.core import Config, State

PRIVATE = 'synthetic-private-response-never-in-errors'


def reply(data=None, *, error=None, ok=True):
    def decode():
        if error:
            raise error
        return data
    return SimpleNamespace(ok=ok, status_code=200 if ok else 503, json=decode)


class Session:
    def __init__(self, completion, receipt=None):
        self.completion, self.receipt = completion, receipt
        self.posts, self.gets = 0, 0
    def post(self, *args, **kwargs):
        self.posts += 1
        assert kwargs['json']['plugins'] == []
        assert set(kwargs['json']['provider']['max_price'].values()) == {0}
        if isinstance(self.completion, BaseException):
            raise self.completion
        return self.completion
    def get(self, *args, **kwargs):
        self.gets += 1
        assert kwargs['timeout'] == 20
        if isinstance(self.receipt, BaseException):
            raise self.receipt
        return self.receipt


def completion(cost=None):
    data = {'id': 'synthetic-generation', 'usage': {}, 'choices': [{'message': {'content': PRIVATE}}]}
    if cost is not None:
        data['usage']['cost'] = cost
    return reply(data)


def model(state, session):
    config = Config(models=['test/model:free'])
    result = FreeModel(config, state, 'synthetic-test-key')
    result.session = session
    result.catalog = {'test/model:free': {'id': 'test/model:free', 'pricing': {'prompt': '0', 'completion': '0'}}}
    return result


@pytest.fixture
def state(tmp_path):
    value = State(tmp_path / 'synthetic.db')
    yield value
    value.close()


def blocked_next(state, session):
    with pytest.raises((ServiceError, RuntimeError)):
        model(state, session).complete(99, 'synthetic', 0, 10)
    assert session.posts == 1
    assert state.call_count(99) == 0


@pytest.mark.parametrize('bad', [None, False, True, {}, [], 'private-bad-cost', 'NaN', float('nan'),
    'Infinity', float('inf'), -1, .01, '1e-999', '1e999'])
@pytest.mark.parametrize('inline', [True, False])
def test_invalid_cost_never_allows_another_post(state, bad, inline):
    session = Session(completion(bad) if inline else completion(), reply({'data': {'total_cost': bad}}))
    if inline and bad is None:
        session.receipt = reply({'data': {}})
    with pytest.raises(ServiceError) as caught:
        model(state, session).complete(1, 'synthetic', 0, 10)
    assert PRIVATE not in str(caught.value) and 'private-bad-cost' not in str(caught.value)
    assert state.call_count(1) == 1 and state.meta('cost_guard_halt')
    json.dumps(state.meta('cost_guard_halt'), allow_nan=False)
    blocked_next(state, session)


@pytest.mark.parametrize('receipt', [requests.Timeout(PRIVATE), reply(error=ValueError(PRIVATE)),
    reply({'data': None}), reply([]), reply({'data': {}}), reply(ok=False)])
def test_receipt_failures_halt_and_survive_sqlite_restart(state, receipt):
    session = Session(completion(), receipt)
    with pytest.raises(ServiceError) as caught:
        model(state, session).complete(1, 'synthetic', 0, 10)
    assert PRIVATE not in str(caught.value)
    reopened = State(state.path)
    try:
        assert reopened.call_count(1) == 1 and reopened.meta('cost_guard_halt')
        blocked_next(reopened, session)
    finally:
        reopened.close()


@pytest.mark.parametrize('response', [reply(error=ValueError(PRIVATE)), reply([]), reply({'usage': None})])
def test_malformed_completion_body_is_halted(state, response):
    session = Session(response)
    with pytest.raises(ServiceError) as caught:
        model(state, session).complete(1, 'synthetic', 0, 10)
    assert PRIVATE not in str(caught.value)
    blocked_next(state, session)


@pytest.mark.parametrize('cost', [0, 0.0, '0', '0.000', '-0', '0e-999'])
@pytest.mark.parametrize('inline', [True, False])
def test_confirmed_zero_clears_guard_only_with_saved_response(state, cost, inline):
    snapshots = []
    state.checkpoint = lambda s: snapshots.append(s.db.serialize())
    session = Session(completion(cost) if inline else completion(), reply({'data': {'total_cost': cost}}))
    m = model(state, session)
    for index in range(2):
        assert m.complete(1, 'synthetic', index, 10)['usage'] is not None
    assert session.posts == 2 and session.gets == (0 if inline else 2)
    assert state.call_count(1) == 2 and state.meta('cost_guard_halt') is None
    assert not state.cost_receipt_halted
    assert len(snapshots) == 4  # Same two durable saves/call as before.
    for i, snapshot in enumerate(snapshots):
        db = sqlite3.connect(':memory:')
        try:
            db.deserialize(snapshot)
            pending = db.execute("SELECT value FROM meta WHERE key='cost_guard_halt'").fetchone()
            assert bool(pending) == (i % 2 == 0)
            if pending:
                assert json.loads(pending[0])['reason'] == 'pending_zero_cost_receipt'
        finally:
            db.close()


@pytest.mark.parametrize('completion_failure', [requests.Timeout(PRIVATE), reply(ok=False)])
def test_handled_completion_transport_policy_is_preserved(state, completion_failure):
    session = Session(completion_failure)
    m = model(state, session)
    for index in range(2):
        with pytest.raises(ServiceError):
            m.complete(1, 'synthetic', index, 10)
    assert session.posts == 2 and state.call_count(1) == 2
    assert not state.meta('cost_guard_halt') and not state.cost_receipt_halted
    assert state.summary()['calls_without_cost_receipt'] == 2


def test_interrupted_request_keeps_prospective_marker_on_restart(state):
    session = Session(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        model(state, session).complete(1, 'synthetic', 0, 10)
    reopened = State(state.path)
    try:
        blocked_next(reopened, session)
    finally:
        reopened.close()


def test_reservation_checkpoint_failure_prevents_send(state):
    def fail(_):
        raise RuntimeError('synthetic checkpoint unavailable')
    state.checkpoint = fail
    session = Session(completion(0))
    with pytest.raises(RuntimeError):
        model(state, session).complete(1, 'synthetic', 0, 10)
    assert session.posts == 0 and state.call_count(1) == 1
    assert state.meta('cost_guard_halt') and state.cost_receipt_halted


def test_failed_zero_receipt_checkpoint_blocks_new_instance_and_durable_restart(state, tmp_path):
    snapshots = []
    def checkpoint(s):
        if snapshots:
            raise RuntimeError('synthetic durable write failed')
        snapshots.append(s.db.serialize())
    state.checkpoint = checkpoint
    session = Session(completion(0))
    with pytest.raises(RuntimeError):
        model(state, session).complete(1, 'synthetic', 0, 10)
    assert state.cost_receipt_halted
    blocked_next(state, session)
    restored = State(tmp_path / 'durable-restart.db')
    restored.db.deserialize(snapshots[0])
    try:
        assert restored.call_count(1) == 1 and restored.meta('cost_guard_halt')
        blocked_next(restored, session)
    finally:
        restored.close()


def test_sqlite_response_write_failure_keeps_pending_guard(state):
    def authorize(action, table, column, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_UPDATE and table == 'calls' else sqlite3.SQLITE_OK
    state.db.set_authorizer(authorize)
    session = Session(completion(0))
    with pytest.raises(sqlite3.DatabaseError):
        model(state, session).complete(1, 'synthetic', 0, 10)
    assert state.meta('cost_guard_halt')
    blocked_next(state, session)


def test_existing_operator_halt_is_never_cleared(state):
    state.set_meta('cost_guard_halt', {'reason': 'operator-review', 'call_id': 5})
    session = Session(completion(0))
    with pytest.raises(ServiceError):
        model(state, session).complete(1, 'synthetic', 0, 10)
    with pytest.raises(RuntimeError):
        state.release_cost_receipt_guard(5)
    assert session.posts == 0 and state.meta('cost_guard_halt')['reason'] == 'operator-review'


def test_call_limits_unchanged(state):
    session = Session(completion(0))
    m = model(state, session)
    for index in range(3):
        m.complete(1, 'synthetic', index, 10)
    with pytest.raises(RuntimeError, match='allowance exhausted'):
        m.complete(1, 'synthetic', 0, 10)
    assert session.posts == 3 and state.call_count(1) == 3


def test_historical_uncertain_reservations_are_retained_without_reinterpretation(state):
    config = Config(models=['test/model:free'])
    old = state.reserve_call(7, 'test/model:free', config)
    session = Session(completion(0))
    model(state, session).complete(1, 'synthetic', 0, 10)
    row = state.db.execute('SELECT status,cost FROM calls WHERE id=?', (old,)).fetchone()
    assert tuple(row) == ('started', None)
    assert state.summary()['reserved_model_calls'] == 2
    assert state.summary()['calls_without_cost_receipt'] == 1
    assert not state.meta('cost_guard_halt')


def test_daily_call_limit_counts_old_uncertainty(state):
    config = Config(models=['test/model:free'])
    for i in range(40):
        state.reserve_call(i, 'test/model:free', config)
    session = Session(completion(0))
    with pytest.raises(RuntimeError, match='allowance exhausted'):
        model(state, session).complete(99, 'synthetic', 0, 10)
    assert session.posts == 0 and state.summary()['reserved_model_calls'] == 40


def test_reservation_transaction_failure_never_sends(state):
    state.db.execute("CREATE TRIGGER fail_receipt_marker BEFORE INSERT ON meta BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END")
    session = Session(completion(0))
    with pytest.raises(sqlite3.DatabaseError):
        model(state, session).complete(1, 'synthetic', 0, 10)
    assert session.posts == 0 and state.call_count(1) == 0


def test_transport_guard_release_checkpoint_failure_stays_halted(state):
    saves = []
    def checkpoint(s):
        if saves:
            raise RuntimeError('synthetic guard release checkpoint failed')
        saves.append(s.db.serialize())
    state.checkpoint = checkpoint
    session = Session(requests.Timeout(PRIVATE))
    with pytest.raises(RuntimeError):
        model(state, session).complete(1, 'synthetic', 0, 10)
    assert state.cost_receipt_halted and state.call_count(1) == 1
    blocked_next(state, session)


def test_unserializable_success_response_keeps_durable_guard(state):
    data = completion(0).json()
    data['synthetic_invalid_field'] = float('nan')
    session = Session(reply(data))
    with pytest.raises(ServiceError):
        model(state, session).complete(1, 'synthetic', 0, 10)
    assert state.meta('cost_guard_halt')
    blocked_next(state, session)


def test_uncertain_checkpoint_that_applied_zero_record_is_safe_after_restore(state, tmp_path):
    saves = []
    def checkpoint(s):
        saves.append(s.db.serialize())
        if len(saves) == 2:
            raise RuntimeError('synthetic write applied but acknowledgement lost')
    state.checkpoint = checkpoint
    session = Session(completion(0))
    with pytest.raises(RuntimeError):
        model(state, session).complete(1, 'synthetic', 0, 10)
    blocked_next(state, session)
    restored = State(tmp_path / 'applied-restart.db')
    restored.db.deserialize(saves[-1])
    try:
        assert restored.meta('cost_guard_halt') is None
        assert restored.completed_calls(1)[0]['cost'] == 0
        assert restored.call_count(1) == 1
    finally:
        restored.close()
