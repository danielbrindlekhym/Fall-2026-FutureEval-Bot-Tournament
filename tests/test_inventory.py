import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from futureeval_bot.api import Metaculus
from futureeval_bot.core import Config, Contract, State, utcnow
import futureeval_bot.__main__ as entry
from futureeval_bot.inventory import release_inventory, inventory_counts

AT = '2026-10-15T12:00:00+00:00'
PAGES = [{'offset':0,'count':1,'has_next':False}]
PRIVATE = 'synthetic-answer-reasoning-credential-canary'


def post(qid=1):
    return {'id':qid+100, 'published_at':'2026-10-13T06:00:00Z', 'private_text':PRIVATE,
        'question':{'id':qid,'type':'binary','title':PRIVATE,'my_forecasts':[PRIVATE],
            'open_time':'2026-10-13T06:00:00Z','scheduled_close_time':'2026-10-13T09:00:00Z',
            'actual_close_time':'2026-10-13T09:00:00Z'}}


def test_only_metadata_is_saved_or_counted():
    row = post()
    result = release_inventory([row], PAGES, AT)
    assert PRIVATE not in json.dumps(result) and PRIVATE not in json.dumps(inventory_counts(result))
    assert result['questions']['1']['latest']['published_at'] == '2026-10-13T06:00:00+00:00'
    assert result['complete_official_released_inventory'] is False


def test_missing_publication_is_not_replaced_by_open_time():
    row = post(); row.pop('published_at')
    result = release_inventory([row], PAGES, AT)
    assert result['questions']['1']['latest']['published_at'] is None
    assert inventory_counts(result)['unknown_publication_questions'] == 1


def test_bad_timestamp_text_is_never_exported():
    row = post(); row['published_at'] = PRIVATE; row['question']['open_time'] = PRIVATE
    assert PRIVATE not in json.dumps(release_inventory([row], PAGES, AT))


def test_disappearing_past_release_is_retained():
    old = release_inventory([post()], PAGES, AT)
    later = release_inventory([], [{'offset':0,'count':0,'has_next':False}], '2026-10-15T13:00:00Z', old)
    assert '1' in later['questions'] and later['visible_question_ids'] == []
    assert later['questions']['1']['first_seen_at'] == AT


def test_changed_deadline_retains_first_and_revision_without_mutating_prior():
    row = post(); old = release_inventory([row], PAGES, AT); frozen = copy.deepcopy(old)
    row['question']['actual_close_time'] = '2026-10-13T08:00:00Z'
    new = release_inventory([row], PAGES, '2026-10-15T13:00:00Z', old)
    saved = new['questions']['1']
    assert old == frozen
    assert saved['first']['actual_close_time'].endswith('09:00:00+00:00')
    assert saved['latest']['actual_close_time'].endswith('08:00:00+00:00')
    assert len(saved['revisions']) == 1 and inventory_counts(new)['changed_metadata_questions'] == 1


def test_revision_history_is_bounded_with_explicit_missingness():
    row = post(); saved = None
    for hour in range(12):
        row['question']['actual_close_time'] = f'2026-10-13T{hour:02}:00:00Z'
        saved = release_inventory([row], PAGES, AT, saved)
    assert len(saved['questions']['1']['revisions']) == 8
    assert saved['questions']['1']['history_truncated'] is True
    assert inventory_counts(saved)['history_truncated_questions'] == 1


def test_empty_next_page_does_not_claim_complete_pagination():
    pages = [{'offset':0,'count':1,'has_next':True},{'offset':1,'count':0,'has_next':True}]
    assert not release_inventory([post()], pages, AT)['pagination_complete']


def test_unknown_groups_and_bad_ids_are_explicit():
    result = release_inventory([{'group_of_questions':{'private':PRIVATE}}, {'id':2},
        {'question':{'id':True}}, post(), post()], PAGES, AT)
    assert result['unrepresented_group_posts'] == 1
    assert result['nonquestion_posts'] == 1
    assert result['invalid_or_duplicate_question_records'] == 2
    assert PRIVATE not in json.dumps(result)


def test_corrupt_saved_schema_is_preserved_for_review():
    old = release_inventory([post()], PAGES, AT)
    old['questions']['1']['latest']['private_response'] = PRIVATE
    with pytest.raises(ValueError, match='schema changed'):
        release_inventory([], [], AT, old)


def test_bad_saved_metadata_is_not_copied():
    old = release_inventory([post()], PAGES, AT)
    old['questions']['1']['latest']['published_at'] = PRIVATE
    with pytest.raises(ValueError, match='schema changed'):
        release_inventory([], [], AT, old)


@pytest.mark.parametrize('field,value', [('first_seen_at',None),('last_seen_at',PRIVATE)])
def test_invalid_saved_observation_times_are_rejected(field, value):
    old = release_inventory([post()], PAGES, AT)
    old['questions']['1'][field] = value
    with pytest.raises(ValueError, match='schema changed'):
        release_inventory([], [], AT, old)


def test_negative_pagination_cannot_look_complete():
    with pytest.raises(ValueError, match='pagination'):
        release_inventory([], [{'offset':0,'count':-1,'has_next':False}], AT)


def test_existing_page_requests_and_return_values_are_unchanged():
    remote = Metaculus.__new__(Metaculus)
    pages = [{'results':[post()], 'next':'synthetic-next'}, {'results':[post(2)],'next':None}]
    calls = []
    def request(method,path,params):
        calls.append((method,path,params.copy()))
        return pages[len(calls)-1]
    remote.request = request
    rows = remote.posts(33121)
    assert len(rows) == 2 and len(calls) == 2
    assert [c[2]['offset'] for c in calls] == [0,1]
    assert all(c[0:2] == ('GET','/posts/') and c[2]['with_cp'] == 'false' for c in calls)
    assert remote.feed_pages == [{'offset':0,'count':1,'has_next':True},{'offset':1,'count':1,'has_next':False}]


def test_existing_empty_page_policy_is_preserved_and_exposed():
    remote = Metaculus.__new__(Metaculus)
    remote.request = lambda *args, **kwargs: {'results':[],'next':'synthetic-next'}
    assert remote.posts(33121) == []
    assert remote.feed_pages == [{'offset':0,'count':0,'has_next':True}]


def test_feed_metadata_is_saved_in_existing_checkpoint_and_projects_stay_separate(tmp_path, monkeypatch):
    state = State(tmp_path / 'synthetic.db')
    remote = SimpleNamespace(feed_pages=PAGES)
    def posts(project):
        row = post(project)
        row['question'].update(title='Synthetic closed question',status='closed')
        return [row]
    remote.posts = posts
    monkeypatch.setattr(entry, 'live_gate', lambda *args:None)
    model = SimpleNamespace(config=Config())
    writes = []
    original = state.set_meta
    def save(key, value):
        writes.append(key)
        original(key,value)
    state.set_meta = save
    try:
        for practice in [True,False,True]:
            result = entry.run(Config(), state, remote, model, practice=practice)
            assert result['release_inventory_counts']['known_questions'] == 1
        metadata = state.meta('full_feed_reconciled')
        assert set(metadata['release_inventories']) == {'33121','32977'}
        assert writes.count('full_feed_reconciled') == 3
        assert 'release_inventory' not in writes  # Uses the existing metadata key.
        assert PRIVATE not in json.dumps(metadata)
        assert state.summary()['reserved_model_calls'] == 0
    finally:
        state.close()


@pytest.mark.parametrize('saved', [
    None, [], {'release_inventories':[]},
    {'release_inventories':{'33121':{'version':2}}},
    {'release_inventories':{'33121':{'version':1,'questions':[]}}},
])
def test_optional_export_error_preserves_prior_metadata_and_forecast_completion(tmp_path, monkeypatch, saved):
    state = State(tmp_path / 'synthetic.db')
    state.set_meta('full_feed_reconciled', saved)
    remote = SimpleNamespace(posts=lambda project:[], feed_pages=[{'offset':0,'count':0,'has_next':False}])
    monkeypatch.setattr(entry, 'live_gate', lambda *args:None)
    try:
        result = entry.run(Config(),state,remote,SimpleNamespace(config=Config()))
        assert result['failures'] == []
        assert result['release_inventory_counts'] == {'status':'unavailable',
            'reason':'metadata_validation_failed','complete_official_released_inventory':False}
        assert state.meta('full_feed_reconciled') == saved
        assert state.meta('last_run')['failures'] == []
        assert state.summary()['reserved_model_calls'] == 0
    finally:
        state.close()


def test_export_does_not_swallow_checkpoint_write_failure(tmp_path, monkeypatch):
    state = State(tmp_path / 'synthetic.db')
    remote = SimpleNamespace(posts=lambda project:[], feed_pages=[{'offset':0,'count':0,'has_next':False}])
    monkeypatch.setattr(entry, 'live_gate', lambda *args:None)
    original = state.set_meta
    def fail(key,value):
        if key == 'full_feed_reconciled':
            raise ValueError('synthetic checkpoint failure')
        original(key,value)
    state.set_meta = fail
    try:
        with pytest.raises(ValueError, match='checkpoint failure'):
            entry.run(Config(),state,remote,SimpleNamespace(config=Config()))
    finally:
        state.close()


@pytest.mark.parametrize('pages', [
    [{'offset':0,'count':1,'has_next':False},{'offset':1,'count':0,'has_next':False}],
    [{'offset':0,'count':2,'has_next':False}],
])
def test_inconsistent_pagination_is_not_complete(pages):
    assert release_inventory([post()],pages,AT)['pagination_complete'] is False


def test_overflowing_utc_conversion_is_missing_metadata():
    row = post(); row['published_at'] = '0001-01-01T00:00:00+01:00'
    result = release_inventory([row],PAGES,AT)
    assert result['questions']['1']['latest']['published_at'] is None


@pytest.mark.parametrize('submission_fails', [False,True])
def test_export_error_preserves_actual_job_success_or_failure(tmp_path, monkeypatch, submission_fails):
    config = Config()
    state = State(tmp_path / 'synthetic.db')
    q = Contract(question_id=71,post_id=171,kind='binary',title='Synthetic question',
        background='Synthetic',resolution='Synthetic',fine_print='',project=config.season_project,
        open_time=(utcnow()-timedelta(hours=1)).isoformat(),
        close_time=(utcnow()+timedelta(hours=1)).isoformat())
    state.discover(q,config)
    state.update(q.question_id,phase='prepared',payload='{}',explanation='Synthetic only')
    saved = {'release_inventories':{'33121':{'version':2}}}
    state.set_meta('full_feed_reconciled',saved)
    monkeypatch.setattr(entry,'live_gate',lambda *args:None)
    handled=[]
    def submit(state,remote,question_id,*,workflow_run_id=None,workflow_run_attempt=None):
        handled.append(question_id)
        if submission_fails:
            raise RuntimeError('Synthetic submission failure')
        state.update(question_id,phase='verified',comment_id=471)
    monkeypatch.setattr(entry,'reconcile_and_submit',submit)
    remote = SimpleNamespace(posts=lambda project:[],feed_pages=[{'offset':0,'count':0,'has_next':False}])
    try:
        result = entry.run(config,state,remote,SimpleNamespace(config=config))
        assert handled == [71]
        assert len(result['failures']) == int(submission_fails)
        assert state.job(71)['phase'] == ('prepared' if submission_fails else 'verified')
        assert result['release_inventory_counts']['status'] == 'unavailable'
        assert state.meta('full_feed_reconciled') == saved
        assert state.meta('last_run')['questions_considered'] == (0 if submission_fails else 1)
        assert state.summary()['reserved_model_calls'] == 0
    finally:
        state.close()


def test_inventory_survives_actual_encrypted_cloud_checkpoint_without_forecasts(tmp_path, monkeypatch):
    import base64
    import sqlite3
    import futureeval_bot.cloud as cloud_module
    state = State(tmp_path / 'synthetic.db')
    state.set_meta('launch_time', AT)
    cloud = cloud_module.CloudState('synthetic/repo', 'synthetic-github-token',
        'synthetic-checkpoint-key-not-real-123456789')
    cloud.sha = 'synthetic-prior-blob'
    cloud.last_signature = cloud_module.signature(state.db)
    writes = []
    def request(method, url, **kwargs):
        assert method == 'PUT' and url.endswith('/contents/progress.enc')
        writes.append(kwargs['json'])
        return SimpleNamespace(ok=True, status_code=200,
            json=lambda: {'content': {'sha':'synthetic-new-blob'}})
    monkeypatch.setattr(cloud.session, 'request', request)
    monkeypatch.setattr(entry, 'live_gate', lambda *args:None)
    state.checkpoint = cloud.save
    remote = SimpleNamespace(posts=lambda project:[], feed_pages=[{'offset':0,'count':0,'has_next':False}])
    restored = sqlite3.connect(':memory:')
    try:
        result = entry.run(Config(), state, remote, SimpleNamespace(config=Config()))
        assert len(writes) == 1  # Feed snapshot requires one actual durable PUT.
        encrypted = base64.b64decode(writes[0]['content'])
        restored.deserialize(cloud_module.unseal(encrypted, cloud.metaculus_token))
        saved = json.loads(restored.execute("SELECT value FROM meta WHERE key='full_feed_reconciled'").fetchone()[0])
        assert saved['release_inventories']['33121']['questions'] == {}
        assert saved['release_inventories']['33121']['pagination_complete'] is True
        assert result['failures'] == [] and state.summary()['reserved_model_calls'] == 0
        state.set_meta('last_run', {'at':'synthetic-later'})
        state.set_meta('service_checks', {'at':'synthetic-later'})
        assert len(writes) == 1
    finally:
        restored.close()
        state.close()


@pytest.mark.parametrize('status', [403, 500])
def test_actual_inventory_checkpoint_rejection_stops_without_retry(tmp_path, monkeypatch, status):
    import futureeval_bot.cloud as cloud_module
    state = State(tmp_path / 'synthetic.db')
    state.set_meta('launch_time', AT)
    cloud = cloud_module.CloudState('synthetic/repo', 'synthetic-github-token',
        'synthetic-checkpoint-key-not-real-123456789')
    cloud.sha = 'synthetic-prior-blob'
    cloud.last_signature = cloud_module.signature(state.db)
    writes = []
    def request(method, url, **kwargs):
        assert method == 'PUT'
        writes.append(kwargs)
        return SimpleNamespace(ok=False, status_code=status)
    monkeypatch.setattr(cloud.session, 'request', request)
    monkeypatch.setattr(entry, 'live_gate', lambda *args:None)
    state.checkpoint = cloud.save
    remote = SimpleNamespace(posts=lambda project:[], feed_pages=[{'offset':0,'count':0,'has_next':False}])
    try:
        with pytest.raises(RuntimeError, match='Durable checkpoint returned HTTP'):
            entry.run(Config(), state, remote, SimpleNamespace(config=Config()))
        assert len(writes) == 1 and state.summary()['reserved_model_calls'] == 0
    finally:
        state.close()
