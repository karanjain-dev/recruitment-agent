import asyncio
import json
import threading
import time

from fastapi.testclient import TestClient

import evals.api as api_module
from evals.api import create_app
from evals.runner import execute_run, prepare_run
from evals.store import Store


def test_local_workbench_end_to_end(tmp_path):
    store = Store(tmp_path / 'results.sqlite3')
    run = prepare_run({'agent_model': 'offline-stub', 'cases': 'READ-01-01', 'runs': 1}, store)
    asyncio.run(execute_run(run['id'], store=store))
    with TestClient(create_app(store)) as client:
        assert client.get('/').status_code == 200
        assert client.get('/assets/app.js').status_code == 200
        bootstrap = client.get('/api/bootstrap').json()
        assert bootstrap['datasets'][0]['case_count'] == 78
        assert 'api_key' not in json.dumps(bootstrap)
        report = client.get('/api/runs/' + run['id']).json()
        assert report['status'] == 'completed'
        listing = client.get('/api/runs').json()[0]
        assert listing['id'] == run['id']
        assert listing['config']['agent_model'] == 'offline-stub'
        assert set(listing['metrics']) <= {'headline', 'cost_speed'}
        assert 'dataset' not in listing and 'settings' not in listing
        assert 'prompts' not in listing['config']
        assert 'cases' not in listing['metrics']
        assert 'prompts' in report['settings']
        case = client.get('/api/runs/' + run['id'] + '/cases/READ-01-01').json()
        assert case['results'][0]['traces'][0]['form']
        export = client.get('/api/runs/' + run['id'] + '/export')
        assert export.status_code == 200 and 'attachment' in export.headers['content-disposition']
        assert 'api_key' not in export.text
        assert client.get('/api/runs/missing').status_code == 404
        assert client.get('/api/datasets/unknown').status_code == 400
        assert client.post('/api/runs', json={'agent_model': 'bad'}).status_code == 400
        assert client.post('/api/runs', json={}, headers={'Origin': 'https://external.test'}).status_code == 403
        assert client.post('/api/runs', json={}, headers={'Content-Length': 'invalid'}).status_code == 400
        assert client.get('/api/health', headers={'Host': 'external.test'}).status_code == 400


def test_calibration_refuses_drafts(tmp_path):
    with TestClient(create_app(Store(tmp_path / 'results.sqlite3'))) as client:
        assert client.post('/api/graders/claims/calibrate', json={}).status_code == 400
        assert client.post('/api/graders/claims/labels', json={}).status_code == 400
        template = client.get('/api/graders/template').json()
        assert len(template['examples']) == 30
        assert all(e['human_label'] is None for e in template['examples'])
        assert client.post('/api/graders/claims/labels', json={'human_reviewed': True, 'labels': template}).status_code == 400


def wait_for_status(client, run_id, terminal=('completed', 'cancelled', 'failed', 'interrupted')):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = client.get('/api/runs/' + run_id).json()
        if run['status'] in terminal:
            return run
        time.sleep(.01)
    raise AssertionError('Background run did not finish: ' + str(run))


def test_launch_endpoint_completes_real_offline_background_run(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, '_api_key', lambda: '')
    store = Store(tmp_path / 'results.sqlite3')
    with TestClient(create_app(store)) as client:
        response = client.post('/api/runs', json={'agent_model': 'offline-stub', 'cases': 'READ-01-01', 'runs': 1})
        assert response.status_code == 200, response.text
        run_id = response.json()['id']
        report = wait_for_status(client, run_id)
        assert report['status'] == 'completed'
        assert report['progress'] == {'completed': 1, 'total': 1}
        result = client.get(f'/api/runs/{run_id}/cases/READ-01-01').json()['results'][0]
        assert result['traces'][0]['delivery_status'] == 'delivered'
        assert result['traces'][0]['next_action'] == 'ask:location'


def test_queue_cancel_is_immediate_and_active_cancel_preserves_current_attempt(tmp_path, monkeypatch):
    from app.runtime import AgentRuntime
    started, release = threading.Event(), threading.Event()
    class SlowRuntime(AgentRuntime):
        async def handle_turn(self, session_id, message):
            started.set()
            while not release.is_set():
                await asyncio.sleep(.005)
            return await super().handle_turn(session_id, message)
    async def controlled_run(run_id, store):
        return await execute_run(run_id, store=store, runtime_factory=SlowRuntime)
    monkeypatch.setattr(api_module, 'execute_run', controlled_run)
    monkeypatch.setattr(api_module, '_api_key', lambda: '')
    store = Store(tmp_path / 'results.sqlite3')
    with TestClient(create_app(store)) as client:
        first = client.post('/api/runs', json={'agent_model': 'offline-stub', 'cases': 'READ-01-01', 'runs': 3}).json()['id']
        assert started.wait(2)
        second = client.post('/api/runs', json={'agent_model': 'offline-stub', 'cases': 'READ-01-01', 'runs': 1}).json()['id']
        assert client.get('/api/runs/' + second).json()['status'] == 'queued'
        assert client.post('/api/runs/' + second + '/cancel').json()['status'] == 'cancelled'
        assert client.get('/api/runs/' + second).json()['finished_at']
        assert store.get_results(second) == []
        assert client.post('/api/runs/' + first + '/cancel').json()['status'] == 'cancelling'
        release.set()
        finished = wait_for_status(client, first)
        assert finished['status'] == 'cancelled'
        assert finished['progress']['completed'] == 1
        assert len(store.get_results(first)) == 1
        assert client.post('/api/runs/missing/cancel').status_code == 404


def test_shutdown_and_restart_recover_both_running_and_queued_jobs(tmp_path, monkeypatch):
    started = threading.Event()
    async def hold_run(run_id, store):
        store.update_run(run_id, status='running')
        started.set()
        await asyncio.Future()
    monkeypatch.setattr(api_module, 'execute_run', hold_run)
    store = Store(tmp_path / 'results.sqlite3')
    with TestClient(create_app(store)) as client:
        first = client.post('/api/runs', json={'agent_model': 'offline-stub', 'cases': 'READ-01-01', 'runs': 1}).json()['id']
        assert started.wait(2)
        second = client.post('/api/runs', json={'agent_model': 'offline-stub', 'cases': 'READ-01-01', 'runs': 1}).json()['id']
    assert store.get_run(first)['status'] == 'interrupted'
    assert store.get_run(second)['status'] == 'interrupted'
    assert store.get_run(first)['finished_at'] and store.get_run(second)['finished_at']
    # An abrupt previous process exit is recovered on startup too.
    orphan = prepare_run({'agent_model': 'offline-stub', 'cases': 'READ-01-01', 'runs': 1}, store)
    with TestClient(create_app(store)) as client:
        assert client.get('/api/runs/' + orphan['id']).json()['status'] == 'interrupted'


def test_calibration_import_validation_and_endpoint_without_provider_calls(tmp_path, monkeypatch):
    from evals.graders import ModelGrader
    judge = ModelGrader({}, api_key='', calibration_path=tmp_path / 'calibration')
    calls = []
    async def calibrate(kind):
        calls.append(kind)
        return {'grader': kind, 'calibrated': False, 'agreement': .8}
    judge.calibrate = calibrate
    monkeypatch.setattr(api_module, 'ModelGrader', lambda *args, **kwargs: judge)
    monkeypatch.setattr(api_module, '_api_key', lambda: 'fake-offline-test-key')
    data = {'label_source': 'human_reviewed', 'reviewed_by': 'Test reviewer', 'examples': [
        {'id': str(i), 'input': {'acknowledgement': 'Thank you.'}, 'human_label': True, 'reviewed': True}
        for i in range(30)]}
    with TestClient(create_app(Store(tmp_path / 'results.sqlite3'))) as client:
        invalid = json.loads(json.dumps(data))
        invalid['examples'][0]['id'] = []
        assert client.post('/api/graders/praise/labels', json={'data': invalid, 'human_reviewed': True}).status_code == 400
        # A label bundle for a different grader must not silently calibrate this one.
        assert client.post('/api/graders/claims/labels', json={'data': data, 'human_reviewed': True}).status_code == 400
        imported = client.post('/api/graders/praise/labels', json={'data': data, 'human_reviewed': True})
        assert imported.status_code == 200 and imported.json()['examples'] == 30
        summary = client.get('/api/graders').json()
        assert next(g for g in summary['graders'] if g['grader'] == 'praise')['reviewed_examples'] == 30
        report = client.post('/api/graders/praise/calibrate').json()
        assert report['agreement'] == .8 and calls == ['praise']
