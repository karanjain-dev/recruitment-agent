"""Regression coverage for missing grader evidence, retries, and saved-run failures."""
import asyncio
import gzip
import json
from pathlib import Path

import httpx
import pytest

from evals.graders.model import ModelGrader
from evals.graders import grade_case
from evals.runner import _cost_and_usage


def response(result=None, *, content=None, finish='stop', refusal=None, usage=True):
    body = {'choices': [{'finish_reason': finish, 'message': {
        'content': json.dumps(result) if content is None else content, 'refusal': refusal}}]}
    if usage:
        body['usage'] = {'prompt_tokens': 10, 'completion_tokens': 5}
    return httpx.Response(200, json=body, headers={'x-request-id': 'req-test'})


def run_grader(tmp_path, replies, payload=None, kind='claims'):
    requests = []

    async def respond(request):
        requests.append(json.loads(request.content))
        reply = replies[min(len(requests) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            grader = ModelGrader({'model': 'gpt-4.1-mini', 'pricing': {'input_per_million': .4, 'output_per_million': 1.6}},
                                 client=client, api_key='sk-test-private', calibration_path=tmp_path)
            return await grader.grade(kind, payload or {'reply': 'The recruiter will confirm.', 'facts': ['Salary is 20000.']})
    return asyncio.run(run()), requests


def test_saved_grader_invented_claims_are_rejected_then_retry_recovers(tmp_path):
    with gzip.open(Path(__file__).resolve().parents[1] / 'evals/baselines/747a06d2d9b7.json.gz', 'rt') as file:
        baseline = json.load(file)
    saved = next(r for r in baseline['results'] if r['case_id'] == 'JOB-01-08')
    old = next(a['actual'] for a in saved['assertions'] if a.get('grader') == 'claims' and a.get('stage') == 'first')
    assert old['claims']
    reply = saved['traces'][0]['delivered']
    result, requests = run_grader(tmp_path, [response({'reason': old['reason'], 'claims': old['claims']}),
                                           response({'reason': 'Only a deferral and a question.', 'claims': []})],
                                  {'reply': reply, 'facts': ['Salary is 20000.']})
    assert result['pass'] and not result['unavailable']
    assert result['attempts'][0]['error_code'] == 'invalid_claim_evidence'
    assert result['attempts'][0]['request_id'] == 'req-test'
    assert result['input_tokens'] == 20 and result['output_tokens'] == 10
    assert result['cost_usd'] == pytest.approx(.000024)
    assert 'pass' not in requests[0]['response_format']['json_schema']['schema']['properties']
    assert len(requests[1]['messages']) == 3


@pytest.mark.parametrize('supported,fact,expected', [(True, 'Shifts are rotational and include nights.', True), (False, None, False)])
def test_claim_verdict_is_computed_and_valid_negative_is_not_retried(tmp_path, supported, fact, expected):
    reply = 'This role includes night shifts.'
    result, requests = run_grader(tmp_path, [response({'reason': 'Evidence assessed.', 'claims': [
        {'claim': reply, 'supported': supported, 'fact': fact}]})],
        {'reply': reply, 'facts': ['Shifts are rotational and include nights.']})
    assert result['pass'] is expected and not result['unavailable']
    assert len(requests) == 1


@pytest.mark.parametrize('claim,fact,supported,error', [
    ('Invented cab service.', None, False, 'invalid_claim_evidence'),
    ('', None, False, 'invalid_claim_evidence'),
    ('Cab provided.', 'Made-up supporting fact', True, 'invalid_fact_evidence'),
    ('Cab provided.', 'Cab provided.', False, 'invalid_fact_evidence'),
])
def test_invalid_evidence_is_not_an_agent_failure(tmp_path, claim, fact, supported, error):
    result, requests = run_grader(tmp_path, [response({'reason': 'Check.', 'claims': [
        {'claim': claim, 'supported': supported, 'fact': fact}]})], {'reply': 'Cab provided.', 'facts': ['Cab provided.']})
    assert result['unavailable'] and not result['pass']
    assert result['error_code'] == error and len(requests) == 2


@pytest.mark.parametrize('reply,code,attempt_count', [
    (httpx.ReadTimeout('SECRET exception body'), 'timeout', 2),
    (httpx.ConnectError('SECRET proxy password'), 'transport_error', 2),
    (httpx.Response(429, json={'error': {'message': 'Rate limited'}}), 'http_error', 2),
    (httpx.Response(503, text='Unavailable'), 'http_error', 2),
    (httpx.Response(401, text='Bad key'), 'http_error', 1),
    (httpx.Response(400, text='Bad schema'), 'http_error', 1),
    (httpx.Response(200, text='<html>bad gateway</html>'), 'invalid_response_json', 2),
    (httpx.Response(200, json={'choices': []}), 'invalid_response_envelope', 2),
    (httpx.Response(200, json=[]), 'invalid_response_envelope', 2),
    (response(content='no JSON'), 'invalid_output_json', 2),
    (response(content='{}'), 'schema_violation', 2),
    (response(content='{"pass":"true","reason":"x"}'), 'schema_violation', 2),
    (response(content='partial', finish='length'), 'incomplete_output', 2),
    (response(content='', refusal='Refused'), 'refusal', 1),
    (response(content='', finish='content_filter'), 'refusal', 1),
])
def test_errors_are_distinct_auditable_and_retries_bounded(tmp_path, reply, code, attempt_count):
    result, requests = run_grader(tmp_path, [reply])
    assert result['unavailable'] and not result['pass'] and not result['calibrated']
    assert result['error_code'] == code
    assert len(requests) == len(result['attempts']) == attempt_count
    assert all(a['error_code'] == code for a in result['attempts'])
    assert 'SECRET' not in json.dumps(result)
    if code == 'incomplete_output':
        assert requests[1]['max_completion_tokens'] > requests[0]['max_completion_tokens']


def test_traces_redact_credentials_and_preserve_safe_provider_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv('EVAL_ACCESS_CODE', 'private-workspace-code')
    result, _ = run_grader(tmp_path, [httpx.Response(401, json={'error': {'message':
        'Rejected sk-test-private private-workspace-code Bearer other-token', 'code': 'invalid_api_key'}})])
    raw = json.dumps(result)
    assert 'private-workspace-code' not in raw and 'sk-test-private' not in raw and 'other-token' not in raw
    assert 'invalid_api_key' in raw and 'raw_response' in raw
    assert '[REDACTED]' in raw


def test_unknown_usage_does_not_become_zero_cost(tmp_path):
    result, _ = run_grader(tmp_path, [httpx.ReadTimeout('timeout'), response({'reason': 'No claims.', 'claims': []})])
    assert result['pass'] and not result['usage_complete'] and result['cost_usd'] is None
    assert result['input_tokens'] == 10
    usage = _cost_and_usage([], None, {'price_table': {'gpt-4.1-mini': {'input_per_million': .4, 'output_per_million': 1.6}}},
                            [{'grader': 'claims', 'actual': result}])
    assert usage['cost_usd'] is None and usage['cost_known_usd'] > 0
    assert not usage['usage_complete'] and not usage['unpriced_models']


def test_failed_grader_diagnostics_survive_assertion_pipeline(tmp_path):
    result, _ = run_grader(tmp_path, [httpx.Response(401, text='Unauthorized')])
    async def grade(kind, payload):
        return result
    rows = asyncio.run(grade_case({'id': 'TEST', 'expected': {'reply': {'claims_supported_by_facts': True}}},
                                  [{'delivered': 'Thanks.'}], {}, None, {}, grade))
    assert rows[0]['unavailable'] and not rows[0]['passed']
    assert rows[0]['actual']['attempts'][0]['http_status'] == 401


def test_missing_key_has_no_attempts_and_no_cost(tmp_path):
    grader = ModelGrader({}, api_key='', calibration_path=tmp_path)
    result = asyncio.run(grader.grade('claims', {'reply': 'Thanks.', 'facts': []}))
    assert result['error_code'] == 'missing_api_key' and result['attempts'] == []
    assert result['cost_usd'] is None


def test_expired_key_is_explained_without_retry(tmp_path):
    result, requests = run_grader(tmp_path, [httpx.Response(401, json={'error': {'code': 'expired_secret_key'}})])
    assert 'expired' in result['reason'] and len(requests) == 1
    assert result['attempts'][0]['provider_error_code'] == 'expired_secret_key'
