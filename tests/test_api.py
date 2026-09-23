"""Behavioral API checks with an isolated durable DB and no provider requests."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import app.main as main
from app.model import ModelError
from app.storage import Answer, History, Interview, Message, Store, Trace, Turn


def key():
    return str(uuid4())


def form_for(criterion='experience', quote='Two years in customer support.', status='complete'):
    return {'answers': [{'criterion': criterion, 'event': 'answer', 'status': status,
                         'value': quote, 'quote': quote, 'yes_no': None, 'implied': False,
                         'condition': None, 'missing_part': 'work_type' if status == 'partial' else None}],
            'candidate_questions': [], 'flag': 'none', 'stop': False,
            'callback': {'requested': False, 'time': None}}


@pytest.fixture
def studio(tmp_path, monkeypatch):
    isolated = Store('sqlite:///' + str(tmp_path / 'interviews.db'))
    control = SimpleNamespace(form=form_for(), understand_calls=0, speak_calls=0,
                              fail_understand=False, fail_speak=False, contexts=[],
                              on_understand=None)

    class FakeGateway:
        async def understand(self, context, emit):
            control.understand_calls += 1
            control.contexts.append(copy.deepcopy(context))
            await emit('model', 'understand', 'started', input=context)
            if control.on_understand:
                control.on_understand()
            if control.fail_understand:
                raise ModelError('Temporary model failure.', code='model_timeout', retryable=True)
            result = copy.deepcopy(control.form)
            await emit('model', 'understand', 'completed', output=result)
            return result

        async def speak(self, saved, facts, unknowns, emit):
            control.speak_calls += 1
            await emit('model', 'speak', 'started', input={'saved': saved})
            if control.fail_speak:
                raise ModelError('Temporary speech failure.', code='model_timeout', retryable=True)
            result = {'acknowledgement': 'Thanks, noted.' if saved else '',
                      'answer_text': ' '.join(f['text'] for f in facts) +
                                     (' The recruiter will confirm.' if unknowns else '')}
            await emit('model', 'speak', 'completed', output=result)
            return result

    monkeypatch.setattr(main, 'store', isolated)
    monkeypatch.setattr(main, 'PRODUCTION', False)
    monkeypatch.setattr(main, 'ACCESS_CODE', 'test-access-code')
    monkeypatch.setattr(main, 'COOKIE_SECRET', 'test-cookie-secret')
    monkeypatch.setattr(main, 'model_ready', lambda: True)
    monkeypatch.setattr(main, 'ModelGateway', FakeGateway)
    main.login_attempts.clear()
    with TestClient(main.app) as client:
        assert client.get('/api/bootstrap').status_code == 200
        assert client.post('/api/login', json={'access_code': 'test-access-code'}).status_code == 200
        yield client, control, isolated
    isolated.engine.dispose()


def create(client, request_id=None, ack=True):
    response = client.post('/api/sessions', json={'request_id': request_id or key()})
    assert response.status_code == 200, response.text
    snapshot = response.json()
    if ack:
        response = client.post(f"/api/sessions/{snapshot['id']}/turns/{snapshot['pending_turn']['id']}/ack")
        assert response.status_code == 200, response.text
        snapshot = response.json()
    return snapshot


def send(client, sid, request_id=None, message='Two years in customer support.'):
    return client.post(f'/api/sessions/{sid}/turns', json={'request_id': request_id or key(), 'message': message})


def by_criterion(snapshot):
    return {row['criterion_id']: row for row in snapshot['answer_sheet']}


def test_bootstrap_auth_health_and_owner_isolation(studio):
    client, _, _ = studio
    bootstrap = client.get('/api/bootstrap').json()
    assert bootstrap['product'] == 'OnlyRound'
    assert bootstrap['authenticated'] is True
    assert bootstrap['auth_required'] is True
    assert bootstrap['harnesses'] and bootstrap['job']['criteria']
    assert client.get('/health').json()['database'] == 'connected'
    created = create(client)
    assert client.get('/api/sessions').json()['sessions'][0]['id'] == created['id']
    with TestClient(main.app) as other:
        other.get('/api/bootstrap')
        assert other.get('/api/sessions').status_code == 401
        assert other.post('/api/login', json={'access_code': 'wrong-é'}).status_code == 401
        assert other.post('/api/login', json={'access_code': 'test-access-code'}).status_code == 200
        assert other.get('/api/sessions').json() == {'sessions': []}
        for path in (f"/api/sessions/{created['id']}", f"/api/sessions/{created['id']}/export"):
            assert other.get(path).status_code == 404
        assert send(other, created['id']).status_code == 404
        assert other.post(f"/api/sessions/{created['id']}/turns/{created['messages'][0]['turn_id']}/ack").status_code == 404
    assert client.post('/api/logout').status_code == 200
    assert client.get('/api/sessions').status_code == 401


def test_greeting_is_idempotent_and_commits_only_after_ack(studio):
    client, _, store = studio
    request_id = key()
    first = create(client, request_id, ack=False)
    second = create(client, request_id, ack=False)
    assert first['id'] == second['id']
    assert first['pending_turn']['id'] == second['pending_turn']['id']
    assert len(second['messages']) == 1
    assert second['state']['current_criterion_id'] is None
    assert second['state']['last_question_asked'] is None
    assert all(row['status'] == 'not_asked' for row in second['answer_sheet'])
    assert second['messages'][0]['delivered'] is False
    assert send(client, first['id']).status_code == 409
    endpoint = f"/api/sessions/{first['id']}/turns/{first['pending_turn']['id']}/ack"
    delivered = client.post(endpoint).json()
    assert delivered['pending_turn'] is None
    assert delivered['state']['current_criterion_id'] == 'experience'
    assert by_criterion(delivered)['experience']['status'] == 'asked'
    assert delivered['messages'][0]['delivered'] is True
    assert client.post(endpoint).json()['events'] == delivered['events']
    with store.read() as db:
        assert db.query(Interview).count() == 1
        assert db.query(Turn).count() == 1


def test_answer_save_precedes_delivery_but_next_question_waits(studio):
    client, control, _ = studio
    initial = create(client)
    request_id = key()
    response = send(client, initial['id'], request_id)
    assert response.status_code == 200, response.text
    result = response.json()
    snapshot = result['snapshot']
    assert result['status'] == 'awaiting_delivery'
    assert snapshot['state']['current_criterion_id'] == 'experience'
    assert snapshot['state']['last_question_asked'] == initial['state']['last_question_asked']
    assert by_criterion(snapshot)['experience']['status'] == 'complete'
    assert by_criterion(snapshot)['location']['status'] == 'not_asked'
    assert len(snapshot['history']) == 1
    assert snapshot['messages'][-1]['delivered'] is False
    assert snapshot['events'] and any(e['name'] == 'understand' for e in snapshot['events'])
    repeated = send(client, initial['id'], request_id).json()
    assert repeated['turn_id'] == result['turn_id']
    assert control.understand_calls == 1
    assert control.speak_calls == 1
    assert send(client, initial['id'], request_id, 'Different payload').status_code == 409
    assert send(client, initial['id']).status_code == 409
    ack = f"/api/sessions/{initial['id']}/turns/{result['turn_id']}/ack"
    delivered = client.post(ack).json()
    assert delivered['state']['current_criterion_id'] == 'location'
    assert by_criterion(delivered)['location']['status'] == 'asked'
    assert delivered['messages'][-1]['delivered'] is True
    assert all(row['phase'] == 'prepare' for row in snapshot['history'])
    assert any(row['phase'] == 'delivery' for row in delivered['history'])
    assert client.post(ack).json() == delivered
    after = send(client, initial['id'], request_id).json()
    assert after['status'] == 'delivered'
    assert after['turn_id'] == result['turn_id']
    assert len(after['snapshot']['messages']) == 3
    assert control.understand_calls == 1


def test_followup_count_changes_only_on_ack(studio):
    client, control, _ = studio
    initial = create(client)
    control.form = form_for(quote='Two years.', status='partial')
    result = send(client, initial['id'], message='Two years.').json()
    before = by_criterion(result['snapshot'])['experience']
    assert before['status'] == 'partial'
    assert before['followups_used'] == 0
    delivered = client.post(f"/api/sessions/{initial['id']}/turns/{result['turn_id']}/ack").json()
    assert by_criterion(delivered)['experience']['followups_used'] == 1


@pytest.mark.parametrize('stage', ['understand', 'speak'])
def test_failed_model_same_key_retry_is_atomic_and_preserves_evidence(studio, stage):
    client, control, store = studio
    initial = create(client)
    request_id = key()
    setattr(control, 'fail_' + stage, True)
    assert send(client, initial['id'], request_id).status_code == 502
    failed = client.get(f"/api/sessions/{initial['id']}").json()
    assert failed['pending_turn']['status'] == 'failed'
    assert failed['state']['current_criterion_id'] == 'experience'
    assert len(failed['messages']) == 2
    assert by_criterion(failed)['experience']['status'] == ('complete' if stage == 'speak' else 'asked')
    assert len(failed['history']) == (1 if stage == 'speak' else 0)
    assert send(client, initial['id']).status_code == 409
    assert send(client, initial['id'], request_id, 'changed').status_code == 409
    setattr(control, 'fail_' + stage, False)
    retried = send(client, initial['id'], request_id)
    assert retried.status_code == 200, retried.text
    result = retried.json()
    assert result['turn_id'] == failed['pending_turn']['id']
    assert len(result['snapshot']['messages']) == 3
    assert len(result['snapshot']['history']) == 1
    assert control.understand_calls == (1 if stage == 'speak' else 2)
    with store.read() as db:
        assert db.query(Message).filter_by(turn_id=result['turn_id'], role='candidate').count() == 1
        assert db.query(History).filter_by(turn_id=result['turn_id'], phase='prepare', accepted=True).count() == 1


def test_duplicate_request_while_processing_reuses_one_turn(studio):
    client, control, _ = studio
    initial = create(client)
    request_id = key()
    started, release = threading.Event(), threading.Event()

    def wait_in_model():
        started.set()
        assert release.wait(timeout=5)

    control.on_understand = wait_in_model
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(send, client, initial['id'], request_id)
        assert started.wait(timeout=5)
        # Sync GET runs in a worker even while this fake provider blocks its loop.
        # Use a separate client portal so the competing request is independent.
        with TestClient(main.app) as competing:
            competing.cookies.update(client.cookies)
            duplicate = send(competing, initial['id'], request_id)
            assert duplicate.status_code == 409
        release.set()
        assert pending.result(timeout=5).status_code == 200
    assert control.understand_calls == 1


def test_concurrent_create_returns_one_session(studio):
    client, _, store = studio
    request_id = key()
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post('/api/sessions', json={'request_id': request_id}), range(4)))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()['id'] for response in responses}) == 1
    with store.read() as db:
        assert db.query(Interview).count() == 1
        assert db.query(Turn).count() == 1


@pytest.mark.parametrize('stale_failure', [False, True])
def test_expired_attempt_cannot_overwrite_its_successful_retry(studio, stale_failure):
    client, control, store = studio
    initial = create(client)
    request_id = key()
    started, release = threading.Event(), threading.Event()

    def hold_first_attempt():
        if control.understand_calls == 1:
            started.set()
            assert release.wait(timeout=5)
            if stale_failure:
                raise ModelError('Expired request failed.', code='model_timeout')

    control.on_understand = hold_first_attempt
    with ThreadPoolExecutor(max_workers=2) as pool:
        original = pool.submit(send, client, initial['id'], request_id)
        assert started.wait(timeout=5)
        with store.tx() as db:
            db.get(Interview, initial['id']).lease_until = 0
        with TestClient(main.app) as retry_client:
            retry_client.cookies.update(client.cookies)
            recovered = send(retry_client, initial['id'], request_id)
            assert recovered.status_code == 200, recovered.text
        release.set()
        assert original.result(timeout=5).status_code == 409
    saved = client.get(f"/api/sessions/{initial['id']}").json()
    assert saved['pending_turn']['id'] == recovered.json()['turn_id']
    assert saved['pending_turn']['status'] == 'awaiting_delivery'
    assert len(saved['messages']) == 3
    assert len(saved['history']) == 1
    assert saved['state']['current_criterion_id'] == 'experience'


def test_export_ids_and_database_duplicate_constraints(studio):
    client, _, store = studio
    initial = create(client)
    result = send(client, initial['id']).json()
    snapshot = client.post(f"/api/sessions/{initial['id']}/turns/{result['turn_id']}/ack").json()
    export = client.get(f"/api/sessions/{initial['id']}/export")
    assert export.status_code == 200
    assert 'attachment;' in export.headers['content-disposition']
    assert export.json()['id'] == initial['id']
    ids = [snapshot['id']]
    for field in ('messages', 'history', 'hints', 'candidate_questions', 'flags', 'events', 'answer_sheet'):
        ids.extend(row['id'] for row in snapshot[field])
    assert len(ids) == len(set(ids))
    for value in ids: assert str(UUID(value)) == value
    with pytest.raises(IntegrityError):
        with store.tx() as db:
            db.add(Answer(session_id=initial['id'], criterion_id='experience', payload={}))
    with pytest.raises(IntegrityError):
        with store.tx() as db:
            db.add(Message(session_id=initial['id'], turn_id=result['turn_id'], role='candidate', content='duplicate'))
    with store.read() as db:
        assert db.query(Answer).filter_by(session_id=initial['id']).count() == len(main.JOB['criteria'])


def test_payload_and_cross_origin_limits(studio):
    client, _, _ = studio
    initial = create(client)
    assert send(client, initial['id'], message=' ').status_code == 422
    assert send(client, initial['id'], message='x' * 6001).status_code == 422
    assert client.post('/api/sessions', json={'request_id': 'invalid'}).status_code == 422
    assert client.post('/api/sessions', json={'request_id': key()}, headers={'origin': 'https://elsewhere.test'}).status_code == 403
    assert client.post('/api/login', content='x', headers={'content-length': 'not-a-number'}).status_code == 400


def test_legacy_schema_gets_attempt_fence_migration(tmp_path):
    legacy = Store('sqlite:///' + str(tmp_path / 'legacy.db'))
    with legacy.engine.begin() as connection:
        connection.execute(text('CREATE TABLE turns (id VARCHAR(36) PRIMARY KEY, session_id VARCHAR(36), request_id VARCHAR(36), candidate_text TEXT, status VARCHAR(32), reply TEXT, result JSON, error TEXT, created_at FLOAT)'))
    legacy.init()
    legacy.init()
    with legacy.read() as db:
        columns = [row[1] for row in db.execute(text('PRAGMA table_info(turns)'))]
        assert columns.count('attempt_token') == 1
    legacy.engine.dispose()


@pytest.mark.parametrize('message,expected', [
    ("I'm only 16.", True), ("I am 17 years old.", True), ("My age is 16", True),
    ("I'm 16 years into my career.", False), ("Her age is 16", False),
    ("I am 18 years old.", False), ("I was 16 then.", False),
    ("I am not 16.", False), ("I have 16 years of experience.", False),
])
def test_underage_corroboration_requires_personal_present_age(message, expected):
    assert main.explicit_underage(message) is expected
