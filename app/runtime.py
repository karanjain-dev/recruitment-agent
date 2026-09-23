"""Reusable conversation hooks with isolated storage and an injectable clock.

The web API and AgentRuntime call the same ``handle_turn`` orchestration. This
module deliberately has no dependency on eval datasets, graders, or expectations.
The existing agent has no hiring Judge: close_and_score reports that missing hook
explicitly, rather than synthesizing hiring decisions in an evaluation adapter.
"""
from __future__ import annotations

import copy
import json
import os
import re
import time
from pathlib import Path

import httpx
from sqlalchemy.engine import make_url

from app.engine import (assemble, build_context, check_reply, decide, fallback_reply,
                        initial_sheet, initial_state, load_job, normalize)
from app.model import ModelError, ModelGateway
from app.storage import (Answer, CandidateQuestion, Flag, Hint, Interview, Message,
                         Store, Trace, Turn, add_history, save_sheet, uid)


async def handle_turn(*, job, state, sheet, hints, candidate_messages, message,
                      message_id, gateway, emit, now, prepared=None, on_prepared=None):
    """The common turn core; callers own transactions and delivery acknowledgement.

    ``on_prepared`` must durably save validated evidence before speech is requested.
    Keeping that callback here preserves the chat API's retry and lease semantics.
    """
    if prepared:
        result = prepared
        await emit('harness', 'Resume prepared turn', 'completed',
                   output={'reused_validated_answers': True})
    else:
        if state['state'] == 'roleplay':
            form = await gateway.classify_roleplay(message, job['roleplay']['persona'],
                                                   state.get('roleplay_last_line', ''), emit)
            form['roleplay_event'] = form.pop('event', form.get('roleplay_event', 'roleplay_break'))
        else:
            context = build_context(job, state, sheet, hints, candidate_messages, message)
            form = await gateway.understand(context, emit)
        normal = normalize(form)
        if 'roleplay_event' in form:
            normal['roleplay_event'] = form['roleplay_event']
        await emit('harness', 'Form normalisation', 'completed', input=form, output=normal)
        result = decide(job, state, sheet, normal, message, message_id, now(), hints=hints)
        if normal['flag'] != 'none':
            await emit('harness', 'Automatic tag routing', 'completed',
                       input={'flag': normal['flag']},
                       output={'flags': result['flags'], 'action': result['action'],
                               'close_reason': result['state'].get('close_reason'),
                               'fixed_response': result['path'] == 'A'})
        if on_prepared is not None:
            await on_prepared(result)
        await emit('tool', 'Answer validator', 'completed',
                   output={'saved': result.get('saved', []), 'history': result.get('history', []),
                           'hints': result.get('hints', [])})
        await emit('tool', 'Job facts lookup', 'completed', input=normal.get('candidate_questions', []),
                   output={'facts': result.get('facts', []), 'unknowns': result.get('unknowns', [])})
        await emit('harness', 'Progression engine', 'completed',
                   output={'action': result.get('action'), 'question': result.get('question'),
                           'proposed_state': result['state'], 'committed': False})
    if result.get('action') == 'roleplay_customer':
        role = await gateway.roleplay_reply(message, job['roleplay']['persona'],
                                            state.get('roleplay_last_line', ''), emit)
        customer = role.get('customer_line', '')
        failed = []
        if not customer or len(customer.split()) > 70 or re.search(
                r'\b(you should|you could|correct answer|interview|candidate|score|assessment|great|perfect|excellent)\b',
                customer, re.I):
            failed = ['roleplay_coaching_or_format']
            customer = job['roleplay']['safe_line']
        await emit('harness', 'Roleplay reply checker', 'completed', input=role,
                   output={'failed_checks': failed, 'customer_line': customer})
        result['question'] = customer
        result['state']['roleplay_last_line'] = customer
        result['state']['last_question_asked'] = customer
    speech = {'acknowledgement': '', 'answer_text': ''}
    if result.get('path') == 'B':
        for attempt in range(2):
            speech = await gateway.speak(result.get('saved', []), result.get('facts', []),
                                         result.get('unknowns', []), emit)
            failures = check_reply(speech, result.get('facts', []), result.get('unknowns', []))
            await emit('harness', 'Reply checker', 'completed', input=speech,
                       output={'attempt': attempt + 1, 'passed': not failures, 'failed_checks': failures})
            if not failures:
                break
        if failures:
            speech = fallback_reply(result.get('facts', []), result.get('unknowns', []))
            await emit('harness', 'Fixed reply fallback', 'completed', output=speech)
    else:
        await emit('harness', 'Fixed wording', 'completed',
                   output={'path': 'A', 'action': result.get('action')})
    return result, assemble(result, speech)


class SessionClock:
    """Deterministic, per-runtime clock; model latency is measured separately."""
    def __init__(self, now: float | None = None):
        self._now = time.time() if now is None else float(now)

    def now(self):
        return self._now

    def advance_minutes(self, minutes):
        if minutes < 0:
            raise ValueError('Clock jumps must be nonnegative.')
        self._now += float(minutes) * 60


def adapt_job(source: dict) -> tuple[dict, list[str]]:
    """Translate external job field names while retaining the actual agent policy.

    The supplied eval job omits roleplay and some operational fixed lines. Those
    are inherited from the existing agent configuration and disclosed in traces.
    Candidate facts and criterion definitions always come from the supplied job.
    """
    job = copy.deepcopy(source)
    baseline = load_job()
    warnings = []
    job.setdefault('id', job.get('job_id', 'unnamed-job'))
    job.setdefault('location', job.get('city', ''))
    job.setdefault('facts', copy.deepcopy(job.get('job_facts', [])))
    job.setdefault('duration_seconds', job.get('limits', {}).get('interview_minutes', 15) * 60)
    supplied_lines = job.get('fixed_lines', {})
    lines = copy.deepcopy(baseline['fixed_lines'])
    aliases = {'identity': 'identity_disclosure', 'manipulation': 'manipulation_reply',
               'outcome': 'outcome_neutral', 'abuse_warn': 'abuse_warning', 'fallback': 'reply_fallback'}
    for key, value in supplied_lines.items():
        lines[aliases.get(key, key)] = value
    inherited = sorted(set(baseline['fixed_lines']) - {aliases.get(k, k) for k in supplied_lines})
    if inherited:
        warnings.append('Existing agent fixed lines used for omitted keys: ' + ', '.join(inherited))
    job['fixed_lines'] = lines
    if 'roleplay' not in job:
        job['roleplay'] = copy.deepcopy(baseline['roleplay'])
        warnings.append('Job omits roleplay configuration; existing agent roleplay is used.')
    for criterion in job['criteria']:
        for key in ('confirm_text', 'confirm_volunteered_text', 'confirm_implied_text'):
            criterion.setdefault(key, None)
    # The current engine owns these constants. Do not pretend a new dataset can
    # alter them without a deliberate agent policy change.
    supported_limits = {'crm_min_minutes_left': 3, 'question_only_streak_limit': 3,
                        'roleplay_turns': 3, 'roleplay_break_limit': 2, 'abuse_limit': 2}
    for key, actual in supported_limits.items():
        if key in job.get('limits', {}) and job['limits'][key] != actual:
            warnings.append(f'Existing agent uses {key}={actual}; supplied {job["limits"][key]} is unsupported.')
    return job, warnings


def _stub_response(request: httpx.Request) -> httpx.Response:
    """Diagnostic provider fixture. Only receives ordinary provider input.

    Deliberately simple: this is not an oracle or a substitute language model.
    It has no access to case ids, expected assertions, or dataset files.
    """
    payload = json.loads(request.content)
    name = payload['response_format']['json_schema']['name'].removeprefix('screening_')
    data = json.loads(payload['messages'][-1]['content'])
    if name == 'understand':
        text = data.get('latest_message', '')
        current = data.get('current_criterion')
        answers, questions = [], []
        if '?' in text:
            questions = [{'text': text, 'type': 'job', 'fact_key': 'unknown'}]
        elif current and text:
            yes_no = ('no' if re.search(r'\b(no|cannot|can.t)\b', text, re.I) else
                      'yes' if re.search(r'\b(yes|willing)\b', text, re.I) else None)
            answers = [{'criterion': current['id'], 'event': 'answer', 'status': 'complete',
                        'value': text, 'quote': text, 'yes_no': yes_no if current.get('is_yes_no') else None,
                        'implied': False, 'condition': None, 'missing_part': None}]
        result = {'answers': answers, 'candidate_questions': questions, 'flag': 'none', 'stop': False,
                  'callback': {'requested': False, 'time': None}}
    elif name == 'speak':
        result = {'acknowledgement': 'Thanks.' if data.get('saved') else '',
                  'answer_text': ' '.join(f['text'] for f in data.get('facts', []))}
        if data.get('unknown_questions'):
            result['answer_text'] += ' The recruiter will confirm the details you asked about.'
    elif name == 'roleplay_classify':
        result = {'event': 'roleplay_reply', 'flag': 'none'}
    else:
        result = {'customer_line': "I'm still worried about the delay. What can you help me do next?"}
    return httpx.Response(200, json={'id': 'diagnostic-stub', 'model': payload['model'],
                                   'choices': [{'finish_reason': 'stop', 'message': {
                                       'content': json.dumps(result, ensure_ascii=False)}}],
                                   'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}})


class AgentRuntime:
    """Isolated adapter around the real conversation orchestrator and Store."""
    def __init__(self, job: dict, db_path: Path, model_config: dict,
                 prompts: dict | None = None, clock=None, stub: bool = False):
        self.job, self.config_warnings = adapt_job(job)
        self.model_config = copy.deepcopy(model_config)
        self.prompts = dict(prompts or {})
        self.clock = clock or SessionClock()
        self.stub = (stub or model_config.get('provider') == 'stub' or model_config.get('stub') is True
                     or model_config.get('diagnostic') is True)
        self.db_path = Path(db_path).resolve()
        # A hook must never default to or be pointed at the production store.
        production_default = Path(__file__).resolve().parent.parent / 'data' / 'AIrecruiter.db'
        if self.db_path == production_default.resolve():
            raise ValueError('AgentRuntime requires an isolated database path, never the main database.')
        configured_url = os.environ.get('DATABASE_URL')
        if configured_url:
            configured = make_url(configured_url)
            if configured.drivername.startswith('sqlite') and configured.database and configured.database != ':memory:':
                if self.db_path == Path(configured.database).resolve():
                    raise ValueError('AgentRuntime cannot use the configured application database.')
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = Store('sqlite:///' + str(self.db_path))
        self.store.init()
        self._client = httpx.AsyncClient(transport=httpx.MockTransport(_stub_response)) if self.stub else None

    def _now(self):
        return float(self.clock.now() if hasattr(self.clock, 'now') else self.clock())

    def _gateway(self):
        config = self.model_config
        return ModelGateway(client=self._client, api_key='diagnostic-stub' if self.stub else config.get('api_key'),
                            model=config.get('model') or config.get('model_id') or config.get('id'),
                            understand_model=config.get('understand_model'), speak_model=config.get('speak_model'),
                            temperature=config.get('temperature'), prompts=self.prompts,
                            timeout=float(config.get('timeout', 25)))

    def seed(self, setup: dict) -> str:
        expected_ids = {c['id'] for c in self.job['criteria']}
        supplied = copy.deepcopy(setup['answer_sheet'])
        if set(supplied) != expected_ids:
            raise ValueError('Seed answer_sheet must contain exactly the job criterion ids.')
        valid_statuses = {'not_asked', 'asked', 'complete', 'conditional', 'declined', 'partial', 'unclear',
                          'needs_confirmation', 'unclear_final', 'unresolved', 'skipped_time', 'unasked', 'off_target'}
        for cid, row in supplied.items():
            if row.get('status') not in valid_statuses:
                raise ValueError(f'Illegal seeded status for {cid}: {row.get("status")}')
            if row.get('status') == 'complete' and not str(row.get('quote') or '').strip():
                raise ValueError(f'Illegal seeded state: complete {cid} requires a quote.')
            if not isinstance(row.get('followups_used', 0), int) or row.get('followups_used', 0) < 0:
                raise ValueError(f'Illegal followups_used for {cid}.')
        current = setup.get('current_criterion')
        if current is not None and current not in expected_ids:
            raise ValueError(f'Unknown current criterion: {current}')
        if setup.get('job') and setup['job'] != self.job['id']:
            raise ValueError('Seed job id does not match the supplied job configuration.')
        elapsed = float(setup.get('elapsed_minutes', 0))
        if elapsed < 0:
            raise ValueError('Elapsed minutes must be nonnegative.')
        now = self._now()
        state = initial_state(self.job, now - elapsed * 60)
        mode = setup.get('mode', 'questions')
        if mode not in {'questions', 'roleplay', 'qa', 'closed'}:
            raise ValueError(f'Unknown seeded mode: {mode}')
        state.update(state='closed' if mode == 'closed' else 'roleplay' if mode == 'roleplay' else 'open',
                     mode='screening' if mode == 'questions' else mode, current_criterion_id=current,
                     pending_action=setup.get('pending_action'), close_reason=setup.get('close_reason'),
                     last_candidate_msg_at=now, roleplay_done=mode in {'qa', 'closed'},
                     roleplay_transcript=copy.deepcopy(setup.get('roleplay_transcript', [])))
        state['counters']['question_only_streak'] = setup.get('question_only_streak', 0)
        state['counters']['unanswered_streak'] = setup.get('unanswered_streak', setup.get('question_only_streak', 0))
        criterion = next((c for c in self.job['criteria'] if c['id'] == current), None)
        state['last_question_asked'] = (criterion.get('confirm_text') if str(setup.get('pending_action', '')).startswith('confirm')
                                        else criterion['question_text']) if criterion else None
        if mode == 'roleplay':
            state['roleplay_last_line'] = self.job['roleplay']['initial_line']
        sheet = initial_sheet(self.job)
        for cid, row in supplied.items():
            sheet[cid].update(row)
        sid = uid()
        with self.store.tx() as db:
            db.add(Interview(id=sid, owner='isolated-evaluation', request_id=uid(), job=self.job,
                             state=state, created_at=now, updated_at=now))
            db.flush()
            save_sheet(db, sid, sheet)
            for offset, previous in enumerate(setup.get('recent_messages', [])):
                text = previous if isinstance(previous, str) else previous.get('text', previous.get('content', ''))
                role = 'candidate' if isinstance(previous, str) else previous.get('sender', previous.get('role', 'candidate'))
                role = 'candidate' if role in {'user', 'candidate'} else 'assistant'
                tid = uid()
                db.add(Turn(id=tid, session_id=sid, request_id=uid(), candidate_text=text if role == 'candidate' else '',
                            status='delivered', created_at=now - 1 + offset * .001))
                db.flush()
                db.add(Message(session_id=sid, turn_id=tid, role=role, content=text, delivered=True,
                               created_at=now - 1 + offset * .001))
            for source_hint in setup.get('hints', []):
                hint = copy.deepcopy(source_hint)
                if isinstance(hint, str):
                    hint = {'criterion_id': current, 'quote': hint}
                hint.setdefault('criterion_id', hint.get('criterion', current))
                hint.setdefault('message_id', uid())
                hint.setdefault('resolved', False)
                db.add(Hint(session_id=sid, criterion_id=hint['criterion_id'],
                            message_id=hint['message_id'], payload=hint))
        return sid

    def snapshot(self, session_id: str) -> dict:
        with self.store.read() as db:
            session = db.get(Interview, session_id)
            if session is None:
                raise ValueError('Unknown evaluation session.')
            state = copy.deepcopy(session.state)
            state['job'] = self.job['id']
            state['answer_sheet'] = {row.criterion_id: copy.deepcopy(row.payload)
                                     for row in db.query(Answer).filter_by(session_id=session_id)}
            state['mode'] = 'questions' if state['mode'] in {'screening', 'crm'} else state['mode']
            state['current_criterion'] = state.get('current_criterion_id')
            state['elapsed_minutes'] = (self._now() - state['started_at']) / 60
            state['question_only_streak'] = state.get('counters', {}).get('question_only_streak', 0)
            state['close_reason'] = 'completed' if state.get('close_reason') == 'normal' else state.get('close_reason')
            messages = db.query(Message).filter_by(session_id=session_id).order_by(Message.created_at, Message.id).all()
            state['recent_messages'] = [{'sender': row.role, 'text': row.content}
                                        for row in messages if row.role == 'candidate']
            state['hints'] = [copy.deepcopy(row.payload) for row in db.query(Hint).filter_by(session_id=session_id)]
            return state

    def _persist_prepared(self, db, sid, tid, mid, result):
        save_sheet(db, sid, result.get('pre_delivery_sheet', result['sheet']))
        add_history(db, sid, tid, result.get('history', []), 'prepare')
        for hint in result.get('hints', []):
            key = dict(session_id=sid, criterion_id=hint['criterion_id'], message_id=hint.get('message_id', mid))
            row = db.query(Hint).filter_by(**key).first()
            if row:
                row.payload = hint
            else:
                db.add(Hint(**key, payload=hint))
        for question in result.get('questions', []):
            db.add(CandidateQuestion(session_id=sid, message_id=mid, question_text=question['text'], payload=question))
        for flag in result.get('flags', []):
            db.add(Flag(session_id=sid, message_id=mid, type=flag['type'], payload=flag))

    async def handle_turn(self, session_id: str, message: str) -> dict:
        if not isinstance(message, str) or not message.strip() or len(message) > 6000:
            raise ValueError('Candidate message must contain between one and 6000 characters.')
        message = message.strip()
        before = self.snapshot(session_id)
        events = []
        tid, mid = uid(), uid()
        now = self._now()
        with self.store.tx() as db:
            session = db.get(Interview, session_id)
            if session.state['state'] == 'closed':
                raise ValueError('This interview is closed.')
            state = copy.deepcopy(session.state)
            sheet = {a.criterion_id: copy.deepcopy(a.payload) for a in db.query(Answer).filter_by(session_id=session_id)}
            hints = [copy.deepcopy(h.payload) for h in db.query(Hint).filter_by(session_id=session_id)]
            candidate_messages = [m.content for m in db.query(Message).filter_by(session_id=session_id, role='candidate')
                                  .order_by(Message.created_at, Message.id)] + [message]
            latest = db.query(Message).filter_by(session_id=session_id).order_by(Message.created_at.desc()).first()
            message_at = max(now, latest.created_at + .001) if latest else now
            db.add(Turn(id=tid, session_id=session_id, request_id=uid(), candidate_text=message, created_at=now))
            db.flush()
            db.add(Message(id=mid, session_id=session_id, turn_id=tid, role='candidate', content=message,
                           delivered=True, criterion_id=state.get('current_criterion_id'), created_at=message_at))

        async def emit(kind, name, status, **payload):
            event = copy.deepcopy(dict(kind=kind, name=name, status=status, **payload))
            events.append(event)
            with self.store.tx() as db:
                db.add(Trace(session_id=session_id, turn_id=tid, kind=kind, name=name, status=status,
                             input=payload.get('input'), output=payload.get('output'),
                             duration_ms=payload.get('duration_ms'),
                             error=json.dumps(payload['error'], ensure_ascii=False) if payload.get('error') else None))

        async def on_prepared(result):
            with self.store.tx() as db:
                self._persist_prepared(db, session_id, tid, mid, result)
                db.get(Turn, tid).result = result

        result, reply, error = {}, '', None
        try:
            await emit('harness', 'Session guard', 'completed', output={'state': state['state'], 'message_id': mid})
            result, reply = await handle_turn(job=self.job, state=state, sheet=sheet, hints=hints,
                                             candidate_messages=candidate_messages, message=message, message_id=mid,
                                             gateway=self._gateway(), emit=emit, now=self._now, on_prepared=on_prepared)
            # Returning a turn from the headless entry point acknowledges delivery.
            # The web API retains its separate browser acknowledgement transaction.
            with self.store.tx() as db:
                session = db.get(Interview, session_id)
                session.state = result['state']
                session.updated_at = self._now()
                save_sheet(db, session_id, result['sheet'])
                add_history(db, session_id, tid, result.get('history', []), 'delivery')
                turn = db.get(Turn, tid)
                turn.result, turn.reply, turn.status = result, reply, 'delivered'
                db.add(Message(session_id=session_id, turn_id=tid, role='assistant', content=reply,
                               delivered=True, created_at=message_at + .0001))
            await emit('harness', 'Delivery commit', 'completed', output={'next_question_committed': True})
        except Exception as exc:
            error = {'code': 'agent_error', 'cause': exc.code if isinstance(exc, ModelError) else type(exc).__name__,
                     'message': str(exc) if isinstance(exc, ModelError) else 'The agent turn failed.'}
            with self.store.tx() as db:
                turn = db.get(Turn, tid)
                turn.status, turn.error = 'failed', error['message']
                result = copy.deepcopy(turn.result or {})
            await emit('harness', 'Turn recovery', 'failed', error=error)
        trace = self._trace(session_id, tid, message, before, result, reply, events)
        if error:
            trace['agent_error'] = error
            trace['error'] = error
        return trace

    def _trace(self, sid, tid, message, before, result, reply, events):
        form, attempts, checks, usage = {}, [], [], []
        for event in events:
            output = event.get('output') or {}
            if event['name'] == 'Form normalisation':
                form = copy.deepcopy(event.get('input') or {})
                form['questions'] = copy.deepcopy(form.get('candidate_questions', []))
            if event['kind'] == 'model' and event['status'] in {'completed', 'failed'}:
                tokens = output.get('usage', {})
                usage.append({'call': event['name'], 'model': output.get('model'),
                              'latency_ms': event.get('duration_ms', 0), 'duration_ms': event.get('duration_ms', 0),
                              'input_tokens': tokens.get('prompt_tokens', 0),
                              'output_tokens': tokens.get('completion_tokens', 0),
                              'total_tokens': tokens.get('total_tokens', 0), 'status': event['status']})
                if event['name'] == 'speak':
                    parsed = output.get('response') if isinstance(output.get('response'), dict) else output.get('parsed_json')
                    parsed = parsed if isinstance(parsed, dict) else None
                    attempts.append({'raw_text': output.get('raw_text', output.get('response')),
                                     'parsed': parsed, 'acknowledgement': parsed.get('acknowledgement', '') if parsed else '',
                                     'text': assemble(result, parsed) if parsed else str(output.get('response') or ''),
                                     'status': event['status']})
            if event['name'] == 'Reply checker':
                names = ['invalid reply structure', 'question in acknowledgement', 'question in answer text',
                         'outcome or praise wording', 'number not present in approved facts',
                         'answer text without a candidate question', 'unknown question not deferred',
                         'reply exceeds 60 words', 'answer text is not grounded in approved facts', 'approved fact omitted']
                checks.append({**copy.deepcopy(output), 'checks': [
                    {'name': name, 'passed': name not in output.get('failed_checks', [])} for name in names]})
        delivered_ack = ''
        for event in events:
            if event['name'] == 'Reply checker':
                delivered_ack = (event.get('input') or {}).get('acknowledgement', '')
            elif event['name'] == 'Fixed reply fallback':
                delivered_ack = (event.get('output') or {}).get('acknowledgement', '')
        decisions = []
        for item in result.get('history', []):
            if item.get('phase', 'prepare') != 'prepare':
                continue
            reason = item.get('reason')
            decision = 'rejected' if not item.get('accepted') else 'repaired' if 'repaired' in (reason or '') else 'accepted'
            decisions.append({**copy.deepcopy(item), 'criterion': item.get('criterion_id'),
                              'decision': decision, 'reason': reason or 'Accepted by the existing agent validator.'})
        recorded = {(row.get('criterion'), row.get('event')) for row in decisions}
        for proposed in form.get('answers', []):
            if (proposed.get('criterion'), proposed.get('event')) not in recorded:
                decisions.append({'criterion': proposed.get('criterion'), 'event': proposed.get('event'),
                                  'decision': 'rejected', 'accepted': False,
                                  'reason': 'The agent did not apply this proposal in the chosen safety or conversation path.',
                                  'proposed': copy.deepcopy(proposed)})
        lookups = [{**copy.deepcopy(q), 'matched_fact_key': q.get('fact_key') or 'unknown'}
                   for q in result.get('questions', [])]
        return {'session_id': sid, 'turn_id': tid, 'message': message, 'form': form,
                'validator': decisions, 'lookups': lookups, 'next_action': self._next_action(result),
                'speak_attempts': attempts, 'reply_checks': checks, 'delivered': reply,
                'delivery_status': 'delivered' if any(e['name'] == 'Delivery commit' and e['status'] == 'completed'
                                                       for e in events) else 'not_delivered',
                'delivered_acknowledgement': delivered_ack, 'state_before': before,
                'state_after': self.snapshot(sid), 'usage': usage, 'events': events,
                'raw_action': result.get('action'), 'diagnostic': self.stub,
                'config_warnings': self.config_warnings}

    def _next_action(self, result):
        action = result.get('action')
        state = result.get('state', {})
        cid = state.get('current_criterion_id')
        if action == 'close':
            reason = state.get('close_reason')
            return 'close:' + ('completed' if reason == 'normal' else str(reason))
        if str(action).startswith('roleplay'):
            return 'roleplay'
        if action == 'qa':
            return 'qa'
        if str(action).startswith('confirm'):
            return 'confirm:' + str(cid)
        if action in {'ask', 'reask', 'followup'}:
            criterion = next((c for c in self.job['criteria'] if c['id'] == cid), {})
            if action == 'reask' and result.get('question') == criterion.get('simple_question_text'):
                action = 'simple_reask'
            return action + ':' + str(cid)
        # Preserve unsupported policy actions as explicit evidence; never relabel
        # a callback pause as a close solely to satisfy an evaluation vocabulary.
        return action

    async def close_and_score(self, session_id: str) -> dict:
        state = self.snapshot(session_id)
        error = {'code': 'agent_error', 'cause': 'missing_verdict_hook',
                 'message': 'The current conversation agent has no Judge or hiring-verdict implementation. '
                            'No score or verdict has been fabricated.'}
        return {'session_id': session_id, 'judge_output': None, 'code_verdict': None, 'verdict': None,
                'verdict_note': '', 'overrides': [], 'state_after': state, 'final_sheet': state['answer_sheet'],
                'close_reason': state.get('close_reason'), 'agent_error': error, 'error': error,
                'usage': [], 'events': [{'kind': 'hook', 'name': 'Close and score', 'status': 'failed', 'error': error}]}

    def close(self):
        self.store.engine.dispose()

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
        self.close()
