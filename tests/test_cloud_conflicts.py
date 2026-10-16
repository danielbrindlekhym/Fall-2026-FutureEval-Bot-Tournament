"""Synthetic checkpoint failures only; all real HTTP is forbidden."""
import base64
import sqlite3

import pytest
import requests

from futureeval_bot.core import Config, State

from futureeval_bot import cloud as candidate
TOKEN = 'synthetic-checkpoint-key-not-a-real-secret-123456789'


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Real network is prohibited in candidate tests')
    monkeypatch.setattr(requests.sessions.Session, 'request', denied)


@pytest.fixture
def setup(tmp_path):
    state = State(tmp_path / 'synthetic.db')
    state.set_meta('launch_time', 'synthetic-launch')
    baseline = candidate.snapshot(state.db)
    cloud = candidate.CloudState('synthetic/repo', 'synthetic-github-key', TOKEN)
    cloud.sha = 'stale-blob'
    cloud.last_signature = candidate.signature(state.db)
    state.set_meta('offline_checks_passed', {'synthetic': 'new-verification'})
    yield state, cloud, baseline
    state.close()


class Response:
    def __init__(self, status, data=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.data = data

    def json(self):
        return self.data


def server(monkeypatch, cloud, remote_bytes, *, second_status=200, get_error=None):
    writes, reads = [], []

    # Conflict policy tests inject at the authenticated-read boundary. Exact
    # immutable HTTP/tree/blob validation is covered by test_immutable_read.
    def authenticated_read():
        reads.append({'params': {'ref': 'bot-state'}})
        if get_error:
            raise RuntimeError('Durable checkpoint connection failed; operation stopped')
        cloud.sha = 'fresh-blob'
        return candidate.unseal(candidate.seal(remote_bytes, TOKEN), TOKEN)
    monkeypatch.setattr(cloud, 'read', authenticated_read)

    def request(method, url, **kwargs):
        assert url == cloud.base + '/contents/progress.enc'
        if method == 'GET':
            reads.append(kwargs)
            assert kwargs['params'] == {'ref': 'bot-state'}
            if get_error:
                raise get_error
            return Response(200, {'sha': 'fresh-blob', 'encoding': 'base64',
                'content': base64.b64encode(candidate.seal(remote_bytes, TOKEN)).decode()})
        assert method == 'PUT'
        writes.append(dict(kwargs['json']))
        assert writes[-1]['branch'] == 'bot-state'
        if len(writes) == 1:
            return Response(409)
        return Response(second_status, {'content': {'sha': 'saved-blob'}})

    monkeypatch.setattr(cloud.session, 'request', request)
    return writes, reads


def test_unchanged_remote_retries_once_with_same_encrypted_reservation(setup, monkeypatch):
    state, cloud, baseline = setup
    writes, reads = server(monkeypatch, cloud, baseline)
    state.checkpoint = cloud.save
    state.reserve_call(1, Config().models[0], Config())
    assert state.call_count(1) == 1
    assert len(writes) == 2 and len(reads) == 1
    assert [w['sha'] for w in writes] == ['stale-blob', 'fresh-blob']
    assert writes[0]['content'] == writes[1]['content']
    saved = sqlite3.connect(':memory:')
    try:
        saved.deserialize(candidate.unseal(base64.b64decode(writes[-1]['content']), TOKEN))
        assert saved.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 1
    finally:
        saved.close()
    assert cloud.sha == 'saved-blob' and not cloud.conflict_halted
    cloud.save(state)
    assert len(writes) == 2


def test_already_persisted_target_requires_no_second_write(setup, monkeypatch):
    state, cloud, _ = setup
    writes, reads = server(monkeypatch, cloud, candidate.snapshot(state.db))
    cloud.save(state)
    assert len(writes) == 1 and len(reads) == 1
    assert cloud.sha == 'fresh-blob'
    assert cloud.last_signature == candidate.signature(state.db)
    cloud.save(state)
    assert len(writes) == 1


@pytest.mark.parametrize('change', ['reservation', 'cost_halt', 'frozen_payload'])
def test_divergent_remote_never_overwritten_even_by_error_checkpoint(setup, monkeypatch, change):
    state, cloud, baseline = setup
    remote = sqlite3.connect(':memory:')
    try:
        remote.deserialize(baseline)
        if change == 'reservation':
            remote.execute("INSERT INTO calls(question_id,day,model,status,created) VALUES(9,'synthetic','synthetic:free','started','synthetic')")
        elif change == 'cost_halt':
            remote.execute("INSERT INTO meta VALUES('cost_guard_halt','true')")
        else:
            remote.execute("INSERT INTO jobs(question_id,project,contract,config,created,updated,payload,explanation) VALUES(9,33121,'{}','{}','synthetic','synthetic','synthetic-frozen-payload','synthetic-explanation')")
        remote.commit()
        changed = remote.serialize()
    finally:
        remote.close()
    writes, _ = server(monkeypatch, cloud, changed)
    with pytest.raises(RuntimeError, match='Remote progress changed'):
        cloud.save(state)
    assert cloud.conflict_halted and len(writes) == 1
    state.set_meta('synthetic_error_record', True)
    with pytest.raises(RuntimeError, match='Unresolved checkpoint conflict'):
        cloud.save(state)
    assert len(writes) == 1


@pytest.mark.parametrize('status', [409, 503])
def test_second_write_failure_latches_and_cannot_be_retried(setup, monkeypatch, status):
    state, cloud, baseline = setup
    writes, reads = server(monkeypatch, cloud, baseline, second_status=status)
    with pytest.raises(RuntimeError):
        cloud.save(state)
    assert cloud.conflict_halted and len(writes) == 2 and len(reads) == 1
    with pytest.raises(RuntimeError, match='Unresolved checkpoint conflict'):
        cloud.save(state)
    assert len(writes) == 2


def test_conflict_reread_timeout_is_uncertainty_not_retry_permission(setup, monkeypatch):
    state, cloud, baseline = setup
    writes, reads = server(monkeypatch, cloud, baseline, get_error=requests.Timeout())
    with pytest.raises(RuntimeError, match='connection failed'):
        cloud.save(state)
    assert cloud.conflict_halted and len(writes) == 1 and len(reads) == 1
    with pytest.raises(RuntimeError):
        cloud.save(state)
    assert len(writes) == 1


def test_corrupt_remote_cannot_authorize_retry(setup, monkeypatch):
    state, cloud, _ = setup
    calls = []
    def request(method, url, **kwargs):
        calls.append(method)
        return Response(409) if method == 'PUT' else Response(200, {
            'sha': 'fresh-blob', 'encoding': 'base64',
            'content': base64.b64encode(b'not-a-valid-encrypted-checkpoint').decode()})
    monkeypatch.setattr(cloud.session, 'request', request)
    with pytest.raises(Exception):
        cloud.save(state)
    assert cloud.conflict_halted and calls == ['PUT', 'GET']
    with pytest.raises(RuntimeError):
        cloud.save(state)
    assert calls == ['PUT', 'GET']


@pytest.mark.parametrize('status', [401, 403, 422, 500])
def test_other_http_failures_do_not_enable_conflict_recovery(setup, monkeypatch, status):
    state, cloud, _ = setup
    methods = []
    def request(method, url, **kwargs):
        methods.append(method)
        return Response(status)
    monkeypatch.setattr(cloud.session, 'request', request)
    with pytest.raises(RuntimeError, match='HTTP ' + str(status)):
        cloud.save(state)
    assert methods == ['PUT']


def test_ordinary_success_still_skips_unchanged_state(setup, monkeypatch):
    state, cloud, _ = setup
    writes = []
    def request(method, url, **kwargs):
        assert method == 'PUT'
        writes.append(kwargs['json'])
        return Response(200, {'content': {'sha': 'saved-blob'}})
    monkeypatch.setattr(cloud.session, 'request', request)
    cloud.save(state)
    cloud.save(state)
    assert len(writes) == 1 and cloud.sha == 'saved-blob'
