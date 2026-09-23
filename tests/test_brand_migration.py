"""Persisted demo branding changes preserve interview continuity and identities."""
import copy
from uuid import uuid4

import pytest

from app.storage import (Answer, CandidateQuestion, Flag, Hint, History, Interview,
                         Message, SchemaVersion, Store, Trace, Turn)


def key():
    return str(uuid4())


@pytest.fixture
def store(tmp_path):
    result = Store('sqlite:///' + str(tmp_path / 'interviews.db'))
    result.init()
    yield result
    result.engine.dispose()


def seed(store, *, greeting="Hello, I'm LegacyStudio, an automated screening assistant.",
         company='LegacyStudio Demo Company'):
    sid, tid, mid = key(), key(), key()
    with store.tx() as db:
        db.add(Interview(id=sid, owner='existing-owner', request_id=key(),
                         job={'company': company, 'fixed_lines': {'greeting': greeting}},
                         state={'current_criterion_id': 'experience', 'status': 'active',
                                'last_question_asked': 'Do you know legacystudio?',
                                'nested': [{'text': 'LEGACYSTUDIO', 'count': 2, 'optional': None}]}))
        db.flush()
        db.add(Turn(id=tid, session_id=sid, request_id=key(), candidate_text='LegacyStudio sounds good.',
                    status='awaiting_delivery', reply='Welcome to LEGACYSTUDIO.',
                    result={'reply': 'Welcome to LegacyStudio.', 'message_id': mid},
                    attempt_token=key(), error='LegacyStudio sample error'))
        db.flush()
        db.add(Message(id=mid, session_id=sid, turn_id=tid, role='assistant', delivered=False,
                       criterion_id='experience', content='Welcome to LegacyStudio.'))
        db.flush()
        common = {'session_id': sid, 'message_id': mid}
        db.add(Answer(session_id=sid, criterion_id='experience',
                      payload={'quote': 'LegacyStudio sounds good.', 'status': 'complete'}))
        db.add(History(**common, turn_id=tid, criterion_id='experience', accepted=True,
                       phase='prepare', payload={'quote': 'LegacyStudio sounds good.'}))
        db.add(Hint(**common, criterion_id='experience', payload={'text': 'Ask about legacystudio.'}))
        db.add(CandidateQuestion(**common, question_text='What is LegacyStudio?',
                                 payload={'answer': 'LegacyStudio is the demo assistant.'}))
        db.add(Flag(**common, type='clarification', payload={'text': 'Explain LegacyStudio.'}))
        db.add(Trace(session_id=sid, turn_id=tid, kind='harness', name='LegacyStudio demo',
                     status='waiting', input={'messages': [{'content': 'LegacyStudio'}]},
                     output={'LegacyStudio': 'LEGACYSTUDIO', 'unchanged': True},
                     error='LegacyStudio demo error'))
    return sid, tid, mid


def records(store):
    with store.read() as db:
        return {
            model.__tablename__: {
                row.id: {column.name: copy.deepcopy(getattr(row, column.name))
                         for column in model.__table__.columns}
                for row in db.query(model).all()
            }
            for model in (Interview, Turn, Message, Answer, History, Hint,
                          CandidateQuestion, Flag, Trace)
        }


def test_rename_preserves_session_identity_delivery_and_audit_records(store):
    sid, tid, mid = seed(store)
    before = records(store)
    assert store.migrate_product_name() == 9
    after = records(store)
    preserved = {'id', 'session_id', 'turn_id', 'message_id', 'request_id', 'owner',
                 'attempt_token', 'criterion_id', 'status', 'role', 'delivered', 'accepted',
                 'phase', 'type', 'kind', 'created_at', 'updated_at', 'active_turn_id',
                 'lease_until', 'duration_ms'}
    for table, rows in before.items():
        assert set(after[table]) == set(rows)
        for rid, old_row in rows.items():
            for field in preserved.intersection(old_row):
                assert after[table][rid][field] == old_row[field]
    with store.read() as db:
        interview = db.get(Interview, sid)
        assert interview.job['company'] == 'AIrecruiter Demo Company'
        assert interview.job['fixed_lines']['greeting'] == "Hello, I'm AIrecruiter, an automated screening assistant."
        assert interview.state['nested'] == [{'text': 'AIrecruiter', 'count': 2, 'optional': None}]
        assert db.get(Turn, tid).candidate_text == 'AIrecruiter sounds good.'
        assert db.get(Turn, tid).result == {'reply': 'Welcome to AIrecruiter.', 'message_id': mid}
        assert db.get(Message, mid).content == 'Welcome to AIrecruiter.'
        assert db.query(Answer).one().payload['quote'] == 'AIrecruiter sounds good.'
        assert db.query(History).one().payload['quote'] == 'AIrecruiter sounds good.'
        assert db.query(Hint).one().payload['text'] == 'Ask about AIrecruiter.'
        assert db.query(CandidateQuestion).one().question_text == 'What is AIrecruiter?'
        assert db.query(Flag).one().payload['text'] == 'Explain AIrecruiter.'
        assert db.query(Trace).one().input == {'messages': [{'content': 'AIrecruiter'}]}
        # Branding in dictionary values changes; schema keys stay stable.
        assert db.query(Trace).one().output == {'LegacyStudio': 'AIrecruiter', 'unchanged': True}
        assert db.get(SchemaVersion, 3) is not None
    assert store.migrate_product_name() == 0
    assert records(store) == after


@pytest.mark.parametrize('greeting,company', [
    ("Hello, I'm LegacyStudio, an automated screening assistant. More instructions.", 'Unrelated Employer'),
    ('Hello, I’m LegacyStudio, an automated screening assistant.', 'Unrelated Employer'),
    ('A custom greeting.', 'LegacyStudio Demo Company'),
])
def test_alias_is_derived_from_known_job_configuration(store, greeting, company):
    _, _, mid = seed(store, greeting=greeting, company=company)
    store.migrate_product_name()
    with store.read() as db:
        assert db.get(Message, mid).content == 'Welcome to AIrecruiter.'


def test_candidate_content_cannot_define_replacement_aliases(store):
    sid, tid, _ = seed(store, greeting="Hello, I'm AIrecruiter, an automated screening assistant.",
                      company='Actual Employer')
    with store.tx() as db:
        db.get(Turn, tid).candidate_text = "Hello, I'm LegacyStudio, an automated screening assistant."
        db.get(Interview, sid).state = {'candidate_quote': 'LegacyStudio Demo Company'}
    before = records(store)
    assert store.migrate_product_name() == 0
    assert records(store) == before


def test_empty_database_can_complete_migration_once(store):
    assert store.migrate_product_name() == 0
    assert store.migrate_product_name() == 0
    with store.read() as db:
        assert [row.version for row in db.query(SchemaVersion).order_by(SchemaVersion.version)] == [1, 2, 3]
