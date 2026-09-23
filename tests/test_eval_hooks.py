"""The eval hooks use real policy, isolated persistence, and observed model calls."""
import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from app.model import ModelGateway
from app.runtime import AgentRuntime, SessionClock
from app.storage import Answer, Interview, Message

DATA = Path(__file__).resolve().parents[1] / 'evals' / 'data'
RECORDED_FORMS = json.loads((Path(__file__).parent / 'fixtures' / 'harness_regressions.json').read_text())['cases']


def inputs():
    job = json.loads((DATA / 'jobs' / 'customer_support_mumbai_v1.json').read_text())
    cases = json.loads((DATA / 'cases.json').read_text())['cases']
    return job, {case['id']: case for case in cases}


@pytest.mark.parametrize('recorded', RECORDED_FORMS, ids=lambda row: row['case_id'])
def test_recorded_failing_forms_pass_original_harness_expectations(tmp_path, recorded):
    """Same model evidence and dataset; only the harness behavior has changed."""
    async def exercise():
        from app.runtime import _stub_response
        from evals.graders.assertions import grade_case
        job, cases = inputs()
        case = cases[recorded['case_id']]
        runtime = AgentRuntime(job, tmp_path / 'replay.db', {'model': 'recorded-form'}, stub=True)
        await runtime._client.aclose()

        def respond(request):
            payload = json.loads(request.content)
            if payload['response_format']['json_schema']['name'] != 'screening_understand':
                return _stub_response(request)
            return httpx.Response(200, json={'model': 'recorded-form', 'choices': [{
                'finish_reason': 'stop', 'message': {'content': json.dumps(recorded['form'])}}],
                'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}})

        runtime._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            sid = runtime.seed(case['setup'])
            trace = await runtime.handle_turn(sid, case['input'])
            assert not trace.get('agent_error')
            checks = await grade_case(case, [trace], trace['state_after'], None, job)
            assert checks and all(c['passed'] for c in checks), checks
            assert '{value}' not in trace['delivered']
            if case['id'] == 'FLAG-01-02':
                assert trace['delivered'] == runtime.job['fixed_lines']['close_underage']
                assert not trace['speak_attempts']
            elif case['id'] == 'READ-04-02':
                assert trace['state_after']['pending_action'] == 'reask'
                assert trace['state_after']['confirmations_asked'] == []
            elif case['id'] == 'READ-05-02':
                assert runtime.job['criteria'][3]['missing_parts']['timeframe'] in trace['delivered']
        finally:
            await runtime.aclose()
    asyncio.run(exercise())


def test_all_seed_fields_survive_and_sessions_are_isolated(tmp_path):
    job, cases = inputs()
    clock = SessionClock(100_000)
    runtime = AgentRuntime(job, tmp_path / 'agent.db', {'model': 'offline-stub'}, clock=clock, stub=True)
    try:
        ids = []
        for case in cases.values():
            setup = case['setup']
            sid = runtime.seed(setup)
            ids.append(sid)
            state = runtime.snapshot(sid)
            assert state['mode'] == setup['mode']
            assert state['elapsed_minutes'] == pytest.approx(setup.get('elapsed_minutes', 0))
            for criterion, row in setup['answer_sheet'].items():
                for field, value in row.items():
                    assert state['answer_sheet'][criterion][field] == value, (case['id'], criterion, field)
            for field in ('current_criterion', 'pending_action', 'question_only_streak', 'close_reason', 'roleplay_transcript'):
                if field in setup:
                    assert state[field] == setup[field]
            if 'recent_messages' in setup:
                assert state['recent_messages'] == setup['recent_messages']
        assert len(ids) == len(set(ids)) == 78
        with runtime.store.read() as db:
            assert db.query(Interview).count() == 78
            assert db.query(Answer).count() == 78 * 5
    finally:
        asyncio.run(runtime.aclose())


def test_smoke_turn_has_full_trace_and_preserves_seeded_evidence(tmp_path):
    async def exercise():
        job, cases = inputs()
        runtime = AgentRuntime(job, tmp_path / 'agent.db', {'model': 'offline-stub'}, stub=True)
        try:
            sid = runtime.seed(cases['READ-01-01']['setup'])
            trace = await runtime.handle_turn(sid, cases['READ-01-01']['input'])
            assert not trace.get('agent_error')
            assert trace['next_action'] == 'ask:location'
            assert trace['state_before']['answer_sheet']['experience']['status'] == 'asked'
            assert trace['state_after']['answer_sheet']['experience']['quote'] == cases['READ-01-01']['input']
            assert trace['validator'][0]['decision'] == 'accepted'
            assert trace['speak_attempts'][0]['parsed']['acknowledgement'] == 'Thanks.'
            assert json.loads(trace['speak_attempts'][0]['raw_text']) == trace['speak_attempts'][0]['parsed']
            assert trace['reply_checks'][0]['passed']
            assert all(check['passed'] for check in trace['reply_checks'][0]['checks'])
            assert {u['call'] for u in trace['usage']} == {'understand', 'speak'}
            assert trace['diagnostic'] is True
            assert trace['delivered'] == trace['speak_attempts'][0]['text']
            other = runtime.seed(cases['READ-01-01']['setup'])
            assert runtime.snapshot(other)['answer_sheet']['experience']['status'] == 'asked'
        finally:
            await runtime.aclose()
    asyncio.run(exercise())


def test_clock_jumps_drive_real_timeout_policy_and_context_keeps_order(tmp_path):
    async def exercise():
        job, cases = inputs()
        runtime = AgentRuntime(job, tmp_path / 'agent.db', {'model': 'offline-stub'},
                               clock=SessionClock(100_000), stub=True)
        try:
            setup = copy.deepcopy(cases['READ-01-01']['setup'])
            setup['elapsed_minutes'] = 14
            sid = runtime.seed(setup)
            await runtime.handle_turn(sid, 'Two years in support')
            await runtime.handle_turn(sid, 'Andheri')
            assert [m['text'] for m in runtime.snapshot(sid)['recent_messages']] == [
                'Two years in support', 'Andheri']
            runtime.clock.advance_minutes(2)
            trace = await runtime.handle_turn(sid, 'Yes, I am willing')
            assert trace['next_action'] == 'close:timeout'
            assert trace['state_after']['close_reason'] == 'timeout'
            assert trace['state_after']['elapsed_minutes'] == 16
            assert trace['state_after']['answer_sheet']['notice_period']['status'] == 'skipped_time'
        finally:
            await runtime.aclose()
    asyncio.run(exercise())


def test_missing_judge_is_explicit_failure_and_never_a_passing_verdict(tmp_path):
    async def exercise():
        job, cases = inputs()
        runtime = AgentRuntime(job, tmp_path / 'agent.db', {'model': 'offline-stub'}, stub=True)
        try:
            sid = runtime.seed(cases['VERDICT-01-02']['setup'])
            verdict = await runtime.close_and_score(sid)
            assert verdict['agent_error']['code'] == 'agent_error'
            assert verdict['agent_error']['cause'] == 'missing_verdict_hook'
            assert verdict['judge_output'] is None
            assert verdict['code_verdict'] is None
            assert verdict['verdict'] is None
            assert verdict['state_after']['roleplay_transcript'] == cases['VERDICT-01-02']['setup']['roleplay_transcript']
        finally:
            await runtime.aclose()
    asyncio.run(exercise())


def test_raw_speak_failures_remain_visible_after_reply_fallback(tmp_path):
    async def exercise():
        from app.runtime import _stub_response
        job, cases = inputs()
        runtime = AgentRuntime(job, tmp_path / 'agent.db', {'model': 'offline-stub'}, stub=True)
        await runtime._client.aclose()
        def respond(request):
            payload = json.loads(request.content)
            if payload['response_format']['json_schema']['name'] != 'screening_speak':
                return _stub_response(request)
            return httpx.Response(200, json={'model': 'offline-stub', 'choices': [{
                'finish_reason': 'stop', 'message': {'content': json.dumps({
                    'acknowledgement': 'Excellent, you are selected!', 'answer_text': ''})}}],
                'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}})
        runtime._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            sid = runtime.seed(cases['READ-01-01']['setup'])
            trace = await runtime.handle_turn(sid, cases['READ-01-01']['input'])
            assert len(trace['speak_attempts']) == 2
            assert all('selected' in attempt['text'] for attempt in trace['speak_attempts'])
            assert all(not check['passed'] for check in trace['reply_checks'])
            assert 'selected' not in trace['delivered']
            assert trace['delivered_acknowledgement'] == 'Thanks.'
            assert sum(u['total_tokens'] for u in trace['usage']) == 10
            assert not trace.get('agent_error')
        finally:
            await runtime.aclose()
    asyncio.run(exercise())


def test_invalid_seed_is_rejected_before_session_write(tmp_path):
    job, cases = inputs()
    runtime = AgentRuntime(job, tmp_path / 'agent.db', {'model': 'offline-stub'}, stub=True)
    try:
        setup = copy.deepcopy(cases['READ-01-01']['setup'])
        setup['answer_sheet']['experience'].update(status='complete', quote='')
        with pytest.raises(ValueError, match='requires a quote'):
            runtime.seed(setup)
        with runtime.store.read() as db:
            assert db.query(Interview).count() == 0
    finally:
        asyncio.run(runtime.aclose())


@pytest.mark.parametrize('failure', ['timeout', 'unexpected', 'invalid_json', 'invalid_schema'])
def test_failed_speak_preserves_model_events_raw_evidence_and_saved_answer(tmp_path, failure):
    async def exercise():
        from app.runtime import _stub_response
        job, cases = inputs()
        runtime = AgentRuntime(job, tmp_path / 'agent.db', {'model': 'offline-stub', 'speak_model': 'test-speaker'}, stub=True)
        await runtime._client.aclose()
        raw = 'not JSON' if failure == 'invalid_json' else '{"acknowledgement":"Noted.","answer_text":"","unexpected":true}'
        def respond(request):
            payload = json.loads(request.content)
            if payload['response_format']['json_schema']['name'] != 'screening_speak':
                return _stub_response(request)
            if failure == 'timeout':
                raise httpx.ReadTimeout('secret-key-must-not-leak', request=request)
            if failure == 'unexpected':
                raise ValueError('secret-key-must-not-leak')
            return httpx.Response(200, json={'model': 'test-speaker', 'choices': [{
                'finish_reason': 'stop', 'message': {'content': raw}}],
                'usage': {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18}})
        runtime._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            sid = runtime.seed(cases['READ-01-01']['setup'])
            trace = await runtime.handle_turn(sid, cases['READ-01-01']['input'])
            assert trace['agent_error']['code'] == 'agent_error'
            assert trace['delivery_status'] == 'not_delivered'
            assert trace['state_after']['answer_sheet']['experience']['status'] == 'complete'
            assert trace['state_after']['answer_sheet']['location']['status'] == 'not_asked'
            assert [(e['name'], e['status']) for e in trace['events'] if e['kind'] == 'model'] == [
                ('understand', 'started'), ('understand', 'completed'), ('speak', 'started'), ('speak', 'failed')]
            failed_usage = next(u for u in trace['usage'] if u['call'] == 'speak')
            assert failed_usage['model'] == 'test-speaker'
            assert len(trace['speak_attempts']) == 1
            assert trace['speak_attempts'][0]['status'] == 'failed'
            if failure in {'invalid_json', 'invalid_schema'}:
                assert trace['speak_attempts'][0]['raw_text'] == raw
                assert failed_usage['total_tokens'] == 18
            if failure == 'invalid_schema':
                assert trace['speak_attempts'][0]['parsed']['unexpected'] is True
            assert 'secret-key-must-not-leak' not in json.dumps(trace)
        finally:
            await runtime.aclose()
    asyncio.run(exercise())


def test_configurable_models_and_prompts_preserve_raw_response_and_usage():
    async def exercise():
        requests, events = [], []
        async def emit(kind, name, status, **payload):
            events.append(dict(kind=kind, name=name, status=status, **payload))
        def respond(request):
            payload = json.loads(request.content)
            requests.append(payload)
            if 'understand' in payload['response_format']['json_schema']['name']:
                result = {'answers': [], 'candidate_questions': [], 'flag': 'none', 'stop': False,
                          'callback': {'requested': False, 'time': None}}
            else:
                result = {'acknowledgement': 'Noted.', 'answer_text': ''}
            return httpx.Response(200, json={'model': payload['model'], 'choices': [
                {'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}],
                'usage': {'prompt_tokens': 13, 'completion_tokens': 7, 'total_tokens': 20}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            gateway = ModelGateway(client=client, api_key='test-secret', model='fallback',
                                   understand_model='understand-choice', speak_model='speak-choice', temperature=.4,
                                   prompts={'understand': 'Understanding experiment.', 'speak': 'Speaking experiment.'})
            await gateway.understand({'latest_message': 'hello'}, emit)
            await gateway.speak([], [], [], emit)
        assert [r['model'] for r in requests] == ['understand-choice', 'speak-choice']
        assert [r['messages'][0]['content'] for r in requests] == ['Understanding experiment.', 'Speaking experiment.']
        assert all(r['temperature'] == .4 for r in requests)
        completed = [e for e in events if e['status'] == 'completed']
        assert all(e['output']['usage']['total_tokens'] == 20 for e in completed)
        assert all(json.loads(e['output']['raw_text']) == e['output']['response'] for e in completed)
        assert 'test-secret' not in json.dumps(events)
    asyncio.run(exercise())


def test_runtime_refuses_default_and_configured_application_database(tmp_path, monkeypatch):
    job, _ = inputs()
    with pytest.raises(ValueError, match='main database'):
        AgentRuntime(job, Path(__file__).resolve().parents[1] / 'data' / 'AIrecruiter.db', {})
    configured = tmp_path / 'production.db'
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///' + str(configured))
    with pytest.raises(ValueError, match='configured application database'):
        AgentRuntime(job, configured, {})
    assert not configured.exists()
