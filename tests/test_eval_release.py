import copy
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from evals.api import create_app
from evals.baselines import seed_baseline
from evals.metrics import compute_metrics
from evals.runner import prepare_run
from evals.scope import accuracy_view
from evals.store import Store


def test_historical_baseline_excludes_only_verdict_checks_and_preserves_evidence(tmp_path):
    store = Store(tmp_path / 'baseline.db')
    seed_baseline(store)
    raw = store.get_results('747a06d2d9b7')
    before = copy.deepcopy(raw)
    run = store.get_run('747a06d2d9b7')
    h = run['metrics']['headline']
    assert (h['passed_cases'], h['total_cases']) == (55, 71)
    assert (h['passed_assertions'], h['assertion_count']) == (287, 316)
    assert (h['form_assertions_passed'], h['form_assertion_count']) == (138, 140)
    assert (h['code_assertions_passed'], h['code_assertion_count']) == (280, 289)
    assert run['original_metrics']['headline']['total_cases'] == 78
    assert raw == before == store.get_results(run['id'])
    carry = next(c for c in run['metrics']['cases'] if c['case_id'] == 'CARRY-04-01')
    assert carry['passed'] and carry['assertions'] == 2
    seed_baseline(store)
    assert len(store.get_results(run['id'])) == 78
    assert store.list_runs()[0]['metrics']['headline'] == h
    assert not store.compare_runs(run['id'], run['id'])['changes']


def test_real_errors_are_not_hidden_by_verdict_exclusion():
    case = {'id': 'mixed', 'run_mode': 'replay', 'runs': 1, 'expected': {'verdict_not': 'fit'}}
    result = {'case_id': 'mixed', 'status': 'error', 'error': {'cause': 'timeout'}, 'assertions': [
        {'path': 'verdict_not', 'scope': 'verdict', 'passed': False},
        {'path': 'agent_error', 'scope': 'agent', 'passed': False, 'actual': {'cause': 'timeout'}}]}
    _, scoped, _ = accuracy_view([case], [result])
    assert scoped[0]['error']['cause'] == 'timeout'
    assert scoped[0]['status'] == 'error'
    assert len(scoped[0]['assertions']) == 1


def test_new_runs_exclude_verdict_modes_without_rewriting_source(tmp_path):
    store = Store(tmp_path / 'results.db')
    run = prepare_run({'agent_model': 'offline-stub', 'runs': 1}, store)
    assert run['progress']['total'] == 71
    assert len(run['settings']['excluded_case_ids']) == 7
    assert all(c['run_mode'] != 'verdict' for c in run['dataset']['cases'])
    with pytest.raises(ValueError, match='Final-verdict'):
        prepare_run({'agent_model': 'offline-stub', 'cases': 'VERDICT-*'}, store)


def test_persistent_documents_keep_imports_immutable_and_reports_replaceable(tmp_path):
    store = Store(tmp_path / 'docs.db')
    store.save_document('datasets', 'one', {'version': 1})
    with pytest.raises(sqlite3.IntegrityError):
        store.save_document('datasets', 'one', {'version': 2})
    store.save_document('calibration_reports', 'claims', {'agreement': .8})
    store.save_document('calibration_reports', 'claims', {'agreement': .9}, replace=True)
    reopened = Store(store.path)
    assert reopened.get_document('datasets', 'one') == {'version': 1}
    assert reopened.list_documents('calibration_reports') == {'claims': {'agreement': .9}}


def test_hosted_startup_requires_storage_and_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('EVAL_HOSTED', '1')
    with pytest.raises(ValueError, match='Hosted evals require'):
        create_app(Store(tmp_path / 'local.db'))


@pytest.mark.parametrize('prefix', ['', '/evals'])
def test_hosted_login_protects_results_and_mutations(tmp_path, monkeypatch, prefix):
    # Use real SQLite persistence behind a fake hosted marker; this test covers
    # HTTP authentication, independently of the provider's PostgreSQL service.
    store = Store(tmp_path / 'auth.db')
    monkeypatch.setattr(store, 'database_url', 'postgresql://test-only')
    # Hosted initialization checks the marker; connections run locally after it.
    monkeypatch.setenv('EVAL_HOSTED', '1')
    monkeypatch.setenv('EVAL_ACCESS_CODE', 'test-access-code')
    monkeypatch.setenv('EVAL_COOKIE_SECRET', 'x' * 32)
    monkeypatch.setenv('EVAL_ALLOWED_HOSTS', 'eval.test')
    app = create_app(store)
    store.database_url = None
    if prefix:
        from fastapi import FastAPI
        parent = FastAPI()
        parent.mount(prefix, app)
        app = parent
    with TestClient(app, base_url='https://eval.test' + prefix + '/') as client:
        assert client.get('api/health').status_code == 200
        assert client.get('api/session').json()['authenticated'] is False
        assert client.get('api/runs').status_code == 401
        assert client.post('api/runs', json={}).status_code == 401
        assert client.get('api/runs/test/export').status_code == 401
        assert client.post('api/login', json={'access_code': 'wrong'}).status_code == 401
        response = client.post('api/login', json={'access_code': 'test-access-code'})
        assert response.status_code == 200
        assert 'HttpOnly' in response.headers['set-cookie'] and 'Secure' in response.headers['set-cookie']
        assert client.get('api/runs').status_code == 200
        assert client.post('api/runs', json={}, headers={'Origin': 'https://evil.test'}).status_code == 403
        assert client.post('api/logout', json={}).status_code == 200
        assert client.get('api/bootstrap').status_code == 401
        for _ in range(5):
            assert client.post('api/login', json={'access_code': 'wrong'}).status_code == 401
        assert client.post('api/login', json={'access_code': 'test-access-code'}).status_code == 429
