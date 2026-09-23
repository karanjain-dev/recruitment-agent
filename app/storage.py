"""Durable relational records. IDs are UUIDs; retries are constrained in the database."""
from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import (Boolean, Column, Float, ForeignKey, Index, Integer, JSON,
                        String, Text, UniqueConstraint, create_engine, event, inspect, text)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()
def uid(): return str(uuid.uuid4())

class SchemaVersion(Base):
    __tablename__ = 'schema_versions'
    version = Column(Integer, primary_key=True)
    applied_at = Column(Float, nullable=False, default=time.time)

class Interview(Base):
    __tablename__ = 'interviews'
    id = Column(String(36), primary_key=True, default=uid)
    owner = Column(String(64), nullable=False, index=True)
    request_id = Column(String(36), nullable=False)
    job = Column(JSON, nullable=False)
    state = Column(JSON, nullable=False)
    active_turn_id = Column(String(36))
    lease_until = Column(Float)
    created_at = Column(Float, nullable=False, default=time.time)
    updated_at = Column(Float, nullable=False, default=time.time)
    __table_args__ = (UniqueConstraint('owner','request_id'),)

class Turn(Base):
    __tablename__ = 'turns'
    id = Column(String(36), primary_key=True, default=uid)
    session_id = Column(String(36), ForeignKey('interviews.id'), nullable=False, index=True)
    request_id = Column(String(36), nullable=False)
    candidate_text = Column(Text, nullable=False, default='')
    status = Column(String(32), nullable=False, default='processing')
    reply = Column(Text)
    result = Column(JSON)
    error = Column(Text)
    attempt_token = Column(String(36))
    created_at = Column(Float, nullable=False, default=time.time)
    __table_args__ = (UniqueConstraint('session_id','request_id'),)

class Message(Base):
    __tablename__ = 'messages'
    id = Column(String(36), primary_key=True, default=uid)
    session_id = Column(String(36), ForeignKey('interviews.id'), nullable=False, index=True)
    turn_id = Column(String(36), ForeignKey('turns.id'), nullable=False)
    role = Column(String(16), nullable=False)
    content = Column(Text, nullable=False)
    criterion_id = Column(String(80))
    delivered = Column(Boolean, nullable=False, default=False)
    created_at = Column(Float, nullable=False, default=time.time)
    __table_args__ = (UniqueConstraint('turn_id','role'),)

class Answer(Base):
    __tablename__ = 'answer_sheet'
    id = Column(String(36), primary_key=True, default=uid)
    session_id = Column(String(36), ForeignKey('interviews.id'), nullable=False, index=True)
    criterion_id = Column(String(80), nullable=False)
    payload = Column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint('session_id','criterion_id'),)

class History(Base):
    __tablename__ = 'answer_history'
    id = Column(String(36), primary_key=True, default=uid)
    session_id = Column(String(36), ForeignKey('interviews.id'), nullable=False, index=True)
    turn_id = Column(String(36), ForeignKey('turns.id'), nullable=False)
    message_id = Column(String(36))
    criterion_id = Column(String(80))
    accepted = Column(Boolean, nullable=False)
    phase = Column(String(16), nullable=False, default='prepare')
    payload = Column(JSON, nullable=False)
    created_at = Column(Float, nullable=False, default=time.time)

# Only accepted candidate saves are unique; rejected proposals remain append-only.
Index('uq_accepted_candidate_answer', History.message_id, History.criterion_id,
      unique=True, sqlite_where=text("accepted = 1 AND phase = 'prepare'"),
      postgresql_where=text("accepted = true AND phase = 'prepare'"))

class Hint(Base):
    __tablename__ = 'hints'
    id = Column(String(36), primary_key=True, default=uid)
    session_id = Column(String(36), ForeignKey('interviews.id'), nullable=False, index=True)
    criterion_id = Column(String(80), nullable=False)
    message_id = Column(String(36), nullable=False)
    payload = Column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint('session_id','criterion_id','message_id'),)

class CandidateQuestion(Base):
    __tablename__ = 'candidate_questions'
    id = Column(String(36), primary_key=True, default=uid)
    session_id = Column(String(36), ForeignKey('interviews.id'), nullable=False, index=True)
    message_id = Column(String(36), nullable=False)
    question_text = Column(Text, nullable=False)
    payload = Column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint('message_id','question_text'),)

class Flag(Base):
    __tablename__ = 'flags'
    id = Column(String(36), primary_key=True, default=uid)
    session_id = Column(String(36), ForeignKey('interviews.id'), nullable=False, index=True)
    message_id = Column(String(36), nullable=False)
    type = Column(String(40), nullable=False)
    payload = Column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint('message_id','type'),)

class Trace(Base):
    __tablename__ = 'runtime_events'
    id = Column(String(36), primary_key=True, default=uid)
    session_id = Column(String(36), ForeignKey('interviews.id'), nullable=False, index=True)
    turn_id = Column(String(36), ForeignKey('turns.id'), nullable=False, index=True)
    kind = Column(String(32), nullable=False)
    name = Column(String(100), nullable=False)
    status = Column(String(24), nullable=False)
    input = Column(JSON)
    output = Column(JSON)
    duration_ms = Column(Float)
    error = Column(Text)
    created_at = Column(Float, nullable=False, default=time.time)

class Store:
    def __init__(self, url=None):
        url = url or os.getenv('DATABASE_URL','sqlite:///./data/onlyround.db')
        if url.startswith('postgres://'): url = 'postgresql+psycopg://' + url[len('postgres://'):]
        elif url.startswith('postgresql://'): url = 'postgresql+psycopg://' + url[len('postgresql://'):]
        self.sqlite = url.startswith('sqlite')
        if self.sqlite: Path('data').mkdir(exist_ok=True)
        kwargs = {'connect_args': {'check_same_thread':False, 'timeout':30}} if self.sqlite else {}
        self.engine = create_engine(url, pool_pre_ping=True, **kwargs)
        if self.sqlite:
            @event.listens_for(self.engine,'connect')
            def pragmas(connection, _):
                connection.execute('PRAGMA foreign_keys=ON')
                connection.execute('PRAGMA journal_mode=WAL')
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def init(self):
        Base.metadata.create_all(self.engine)
        with self.tx() as db:
            if not db.get(SchemaVersion,1): db.add(SchemaVersion(version=1))
            if not db.get(SchemaVersion,2):
                columns={item['name'] for item in inspect(db.connection()).get_columns('turns')}
                if 'attempt_token' not in columns:
                    db.execute(text('ALTER TABLE turns ADD COLUMN attempt_token VARCHAR(36)'))
                db.add(SchemaVersion(version=2))

    @contextmanager
    def tx(self):
        with self.Session() as db:
            try:
                if self.sqlite: db.execute(text('BEGIN IMMEDIATE'))
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def read(self): return self.Session()

def save_sheet(db, session_id, sheet):
    rows = {a.criterion_id:a for a in db.query(Answer).filter_by(session_id=session_id)}
    for key,value in sheet.items():
        if key in rows: rows[key].payload = value
        else: db.add(Answer(session_id=session_id,criterion_id=key,payload=value))

def add_history(db, session_id, turn_id, entries, phase):
    for item in entries:
        if item.get('phase','prepare') != phase: continue
        db.add(History(session_id=session_id,turn_id=turn_id,
                       message_id=item.get('message_id'),criterion_id=item.get('criterion_id'),
                       accepted=bool(item.get('accepted')),phase=phase,payload=item))
