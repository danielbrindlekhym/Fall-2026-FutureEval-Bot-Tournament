import sqlite3

import pytest
from cryptography.fernet import InvalidToken

from futureeval_bot.cloud import CloudState, seal, signature, snapshot, unseal
from futureeval_bot.core import Config, State

TOKEN = 'synthetic-test-token-not-a-secret-123456789'


def test_checkpoint_encrypts_authenticates_and_roundtrips(tmp_path):
    state = State(tmp_path / 'bot.db')
    state.set_meta('private_note', 'Synthetic confidential reasoning')
    data = snapshot(state.db)
    encrypted = seal(data, TOKEN)
    assert b'Synthetic confidential reasoning' not in encrypted
    assert b'SQLite' not in encrypted
    assert unseal(encrypted, TOKEN) == data
    with pytest.raises(InvalidToken):
        unseal(encrypted, TOKEN + 'wrong-key')
    with pytest.raises(InvalidToken):
        unseal(encrypted[:-10] + b'corruption', TOKEN)
    state.close()


def test_volatile_checks_do_not_create_checkpoint_commits(tmp_path):
    state = State(tmp_path / 'bot.db')
    before = signature(state.db)
    state.set_meta('service_checks', {'at':'synthetic'})
    assert signature(state.db) == before
    state.set_meta('cost_guard_halt', {'call_id':1})
    assert signature(state.db) != before
    state.close()


def test_checkpoint_failure_stops_call_before_model_request(tmp_path):
    state = State(tmp_path / 'bot.db')
    def broken(_):
        raise RuntimeError('Synthetic checkpoint service unavailable')
    state.checkpoint = broken
    with pytest.raises(RuntimeError):
        state.reserve_call(1, Config().models[0], Config())
    assert state.call_count(1) == 1
    state.close()


def test_cloud_compare_and_swap_updates_sha_and_skips_unchanged(tmp_path):
    state = State(tmp_path / 'bot.db')
    cloud = CloudState('synthetic/repo','synthetic-github-token',TOKEN)
    cloud.sha, cloud.last_signature = 'old-sha', signature(state.db)
    calls = []
    def request(method,path,**kwargs):
        calls.append(kwargs['json'])
        assert kwargs['json']['sha'] == 'old-sha'
        return {'content':{'sha':'new-sha'}}
    cloud.request = request
    state.checkpoint = cloud.save
    state.set_meta('service_checks', {'at':1})
    assert calls == []
    state.reserve_call(1,Config().models[0],Config())
    assert len(calls) == 1 and cloud.sha == 'new-sha'
    cloud.save(state)
    assert len(calls) == 1
    state.close()
