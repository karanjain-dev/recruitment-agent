"""Evaluation workbench with local mode and a protected hosted mode."""
from __future__ import annotations

import asyncio
import json
import os
import hmac
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from evals.config import get_default_prompts, get_grader_config, list_model_configs
from evals.datasets import DatasetError, get_dataset, import_dataset, list_datasets
from evals.graders import ModelGrader
from evals.runner import _api_key, execute_run, prepare_run
from evals.store import Store, now
from evals.auth import COOKIE, TTL, token, valid

ROOT = Path(__file__).parent
KINDS = {'claims', 'praise', 'quote', 'must_not_claim'}


def create_app(store: Store | None = None):
    store = store or Store()
    hosted = os.environ.get('EVAL_HOSTED') == '1'
    access_code = os.environ.get('EVAL_ACCESS_CODE', '')
    cookie_secret = os.environ.get('EVAL_COOKIE_SECRET', '')
    if hosted and (len(access_code) < 12 or len(cookie_secret) < 32 or not store.database_url):
        raise ValueError('Hosted evals require a separate PostgreSQL database, access code and cookie secret.')
    hosts = ['127.0.0.1', 'localhost', '[::1]', 'testserver']
    if hosted:
        hosts += ['healthcheck.railway.app']
        hosts += [x.strip() for x in os.environ.get('EVAL_ALLOWED_HOSTS', os.environ.get('RAILWAY_PUBLIC_DOMAIN', '')).split(',') if x.strip()]
    login_attempts = {}
    workers: dict[str, asyncio.Task] = {}
    gate = None

    async def run_job(run_id):
        try:
            async with gate:
                run = store.get_run(run_id)
                if run['status'] == 'cancelled' or run.get('cancel_requested'):
                    store.update_run(run_id, status='cancelled', finished_at=now())
                    return
                await execute_run(run_id, store=store)
        except asyncio.CancelledError:
            run = store.get_run(run_id)
            if run.get('cancel_requested'):
                store.update_run(run_id, status='cancelled', finished_at=now())
            elif run['status'] not in {'completed', 'failed', 'cancelled'}:
                store.update_run(run_id, status='interrupted', finished_at=now(),
                                 error='The evaluation service stopped. Completed results are preserved.')
            raise
        except Exception as exc:
            store.update_run(run_id, status='failed', finished_at=now(),
                             error=f'Evaluation could not complete ({type(exc).__name__}). Completed results are preserved.')

    @asynccontextmanager
    async def lifespan(app):
        nonlocal gate
        gate = asyncio.Semaphore(1)
        if hosted and os.environ.get('EVAL_SEED_BASELINE') == '1':
            from evals.baselines import seed_baseline
            seed_baseline(store)
        store.recover_interrupted()
        yield
        pending = list(workers.items())
        for _, worker in pending:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*(worker for _, worker in pending), return_exceptions=True)
        # A task cancelled before its coroutine started cannot execute its own
        # exception handler. Finalize those queued records as well.
        for run_id, _ in pending:
            run = store.get_run(run_id)
            if run['status'] in {'queued', 'running', 'cancelling'}:
                store.update_run(run_id, status='cancelled' if run.get('cancel_requested') else 'interrupted',
                                 finished_at=now(), error=None if run.get('cancel_requested') else
                                 'The evaluation service stopped. Completed results are preserved.')

    app = FastAPI(title='AIrecruiter Evaluations', lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    app.state.store = store

    @app.middleware('http')
    async def local_boundary(request: Request, call_next):
        # Mounted applications retain their full URL path in the ASGI scope.
        root_path = request.scope.get('root_path', '')
        route_path = request.url.path.removeprefix(root_path) if root_path else request.url.path
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            origin = request.headers.get('origin')
            if origin and urlparse(origin).netloc != request.headers.get('host'):
                return JSONResponse({'detail': 'Use the local evaluation app to make changes.'}, status_code=403)
            if request.headers.get('sec-fetch-site') == 'cross-site':
                return JSONResponse({'detail': 'Cross-site changes are not allowed.'}, status_code=403)
            try:
                content_length = int(request.headers.get('content-length', '0') or 0)
            except ValueError:
                return JSONResponse({'detail': 'Invalid request length.'}, status_code=400)
            if content_length < 0:
                return JSONResponse({'detail': 'Invalid request length.'}, status_code=400)
            if content_length > 12 * 1024 * 1024:
                return JSONResponse({'detail': 'Request exceeds 12 MB.'}, status_code=413)
        if (hosted and route_path.startswith('/api/')
                and route_path not in {'/api/health', '/api/session', '/api/login'}
                and not valid(request.cookies.get(COOKIE), cookie_secret)):
            return JSONResponse({'detail': 'Sign in to the evaluation workspace.'}, status_code=401)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'"
        if route_path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({'detail': str(exc).strip("'")}, status_code=404)

    def grader():
        return ModelGrader(get_grader_config(), api_key=_api_key())

    def summary(run):
        # The history page polls this endpoint. Full frozen datasets, prompts,
        # per-case assertions and slice tables belong in detail/export responses.
        config = run.get('settings', run.get('config', {}))
        compact_config = {key: config[key] for key in (
            'agent_model', 'dataset_id', 'understand_model', 'speak_model', 'temperature', 'runs') if key in config}
        versions = {key: value for key, value in run.get('versions', {}).items() if key in {
            'agent_model_config', 'model_id', 'understand_model', 'speak_model', 'temperature',
            'job_id', 'job_version', 'dataset_version', 'grader_model_id'}}
        metrics = {key: value for key, value in run.get('metrics', {}).items() if key in {'headline', 'cost_speed'}}
        return {**{key: run.get(key) for key in ('id', 'name', 'status', 'diagnostic', 'created_at',
                                               'updated_at', 'started_at', 'finished_at', 'progress', 'error')},
                'agent_model': config.get('agent_model'), 'dataset_version': versions.get('dataset_version'),
                'config': compact_config, 'versions': versions, 'metrics': metrics}

    @app.get('/api/health')
    def health():
        try:
            with store.connect() as db:
                db.execute('SELECT 1').fetchone()
        except Exception:
            return JSONResponse({'status': 'unavailable'}, status_code=503)
        return {'status': 'ok', 'local_only': not hosted}

    @app.get('/api/session')
    def session(request: Request):
        return {'hosted': hosted, 'authenticated': not hosted or valid(request.cookies.get(COOKIE), cookie_secret),
                'storage': 'PostgreSQL' if store.database_url else 'SQLite'}

    @app.post('/api/login')
    def login(request: Request, body: dict):
        if not hosted:
            return {'authenticated': True}
        address = request.client.host if request.client else 'unknown'
        stamp = time.monotonic()
        attempts = [v for v in login_attempts.get(address, []) if stamp - v < 60]
        login_attempts[address] = attempts
        if len(attempts) >= 5:
            raise HTTPException(429, 'Too many attempts. Try again in a minute.')
        provided = body.get('access_code')
        if not isinstance(provided, str) or not hmac.compare_digest(provided.encode(), access_code.encode()):
            attempts.append(stamp)
            raise HTTPException(401, 'Incorrect access code.')
        login_attempts.pop(address, None)
        response = JSONResponse({'authenticated': True})
        response.set_cookie(COOKIE, token(cookie_secret), max_age=TTL, httponly=True, secure=True, samesite='strict')
        return response

    @app.post('/api/logout')
    def logout():
        response = JSONResponse({'authenticated': False})
        response.delete_cookie(COOKIE, secure=hosted, httponly=True, samesite='strict')
        return response

    @app.get('/api/bootstrap')
    def bootstrap():
        from evals.baselines import baseline_report
        return {'models': list_model_configs(), 'prompts': get_default_prompts(),
                'datasets': [{k: v for k, v in d.items() if k not in {'cases', 'job', 'taxonomy'}} for d in list_datasets()],
                'model_ready': bool(_api_key()), 'grader_config': get_grader_config(),
                'hosted': hosted, 'storage': 'PostgreSQL' if store.database_url else 'SQLite',
                'baseline': store.get_document('release', 'baseline') or baseline_report()}

    @app.get('/api/datasets')
    def datasets():
        return [{k: v for k, v in d.items() if k not in {'cases', 'job', 'taxonomy'}} for d in list_datasets()]

    @app.get('/api/datasets/{dataset_id}')
    def dataset(dataset_id: str):
        return get_dataset(dataset_id)

    @app.post('/api/datasets')
    def add_dataset(body: dict):
        dataset = import_dataset(body)
        return {k: v for k, v in dataset.items() if k not in {'cases', 'job', 'taxonomy'}}

    @app.get('/api/runs')
    def runs():
        return [summary(run) for run in store.list_runs()]

    @app.post('/api/runs')
    async def start_run(body: dict):
        run = prepare_run(body, store=store)
        if not run['diagnostic'] and not _api_key():
            store.update_run(run['id'], status='failed', finished_at=now(), error='Model API key is not configured.')
            raise HTTPException(503, 'Add OPENAI_API_KEY to the local .env file, or choose an offline diagnostic.')
        task = asyncio.create_task(run_job(run['id']))
        workers[run['id']] = task
        task.add_done_callback(lambda done: workers.pop(run['id'], None))
        return summary(run)

    @app.get('/api/runs/{run_id}')
    def run_detail(run_id: str):
        run = store.get_run(run_id)
        if not run:
            raise KeyError('Evaluation not found')
        return run

    @app.post('/api/runs/{run_id}/cancel')
    async def cancel(run_id: str):
        current = store.get_run(run_id)
        if current is None:
            raise KeyError('Evaluation not found')
        store.request_cancel(run_id)
        if current['status'] == 'queued':
            # A waiting run has no in-flight evidence to preserve. Remove it from
            # the queue immediately instead of waiting for earlier work to finish.
            store.update_run(run_id, status='cancelled', finished_at=now())
            task = workers.get(run_id)
            if task is not None:
                task.cancel()
        return {'status': store.get_run(run_id)['status']}

    @app.get('/api/runs/{run_id}/cases/{case_id}')
    def case_detail(run_id: str, case_id: str):
        return store.get_case_detail(run_id, case_id)

    def download(payload, filename):
        return Response(json.dumps(payload, indent=2, ensure_ascii=False), media_type='application/json',
                        headers={'Content-Disposition': f'attachment; filename="{filename}"'})

    @app.get('/api/runs/{run_id}/export')
    def export_run(run_id: str):
        return download({'run': run_detail(run_id), 'results': store.get_results(run_id)}, f'evaluation-{run_id}.json')

    @app.get('/api/runs/{run_id}/cases/{case_id}/export')
    def export_case(run_id: str, case_id: str):
        return download(store.get_case_detail(run_id, case_id), f'trace-{run_id}-{case_id}.json')

    @app.get('/api/compare')
    def compare(a: str, b: str):
        return store.compare_runs(a, b)

    @app.get('/api/graders')
    def graders():
        return {'config': get_grader_config(), 'graders': grader().calibration_summary()}

    @app.get('/api/graders/template')
    def calibration_template():
        return download({'label_source': 'human_reviewed', 'reviewed_by': 'REPLACE_WITH_REVIEWER_NAME', 'examples': [
            {'id': f'example-{i+1:02}', 'input': {'reply': '', 'facts': []}, 'human_label': None, 'reviewed': False}
            for i in range(30)], 'instructions': 'Use reply/facts for claims, acknowledgement for praise, value/quote for quote, reply/forbidden_claims for must_not_claim. Replace human_label with true or false; set reviewed only after human review.'}, 'human-calibration-template.json')

    @app.post('/api/graders/{kind}/labels')
    def labels(kind: str, body: dict):
        if kind not in KINDS:
            raise ValueError('Unknown grader')
        data = body.get('data')
        if body.get('human_reviewed') is not True or not isinstance(data, dict):
            raise ValueError('Confirm human review and provide a labelled examples object.')
        reviewer = data.get('reviewed_by')
        if data.get('label_source') != 'human_reviewed' or not isinstance(reviewer, str) or not reviewer.strip() or reviewer == 'REPLACE_WITH_REVIEWER_NAME':
            raise ValueError('Set label_source to human_reviewed and identify the reviewer in reviewed_by.')
        examples = data.get('examples', [])
        if not isinstance(examples, list) or len(examples) < 30:
            raise ValueError('Provide at least 30 human-labelled examples.')
        if any(not isinstance(ex, dict) or ex.get('reviewed') is not True or type(ex.get('human_label')) is not bool or not isinstance(ex.get('input'), dict) or not ex['input'] for ex in examples):
            raise ValueError('Every example needs a nonempty input, a boolean human_label, and reviewed: true.')
        if any(not isinstance(ex.get('id'), str) or not ex['id'].strip() for ex in examples):
            raise ValueError('Every calibration example needs a unique, nonempty string ID.')
        if len({ex['id'] for ex in examples}) != len(examples):
            raise ValueError('Every calibration example needs a unique ID.')
        text_fields = {'claims': ('reply',), 'praise': ('acknowledgement',),
                       'quote': ('criterion', 'value', 'quote'), 'must_not_claim': ('reply',)}
        list_fields = {'claims': 'facts', 'must_not_claim': 'forbidden_claims'}
        for example in examples:
            data_input = example['input']
            if any(not isinstance(data_input.get(field), str) for field in text_fields[kind]):
                raise ValueError(f'{kind} examples require text fields: ' + ', '.join(text_fields[kind]))
            if kind in list_fields:
                field = list_fields[kind]
                if not isinstance(data_input.get(field), list) or any(not isinstance(item, str) for item in data_input[field]):
                    raise ValueError(f'{kind} examples require {field} as a list of text strings.')
        if store.database_url:
            store.save_document('calibration_labels', kind, data, replace=True)
            return {'status': 'imported', 'examples': len(examples)}
        destination = grader().calibration_dir / f'{kind}.json'
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        return {'status': 'imported', 'examples': len(examples)}

    @app.post('/api/graders/{kind}/calibrate')
    async def calibrate(kind: str):
        if kind not in KINDS:
            raise ValueError('Unknown grader')
        judge = grader()
        item = next(row for row in judge.calibration_summary() if row['grader'] == kind)
        if item['reviewed_examples'] < 30:
            raise ValueError('Import at least 30 human-reviewed labels before calibration.')
        if not _api_key():
            raise ValueError('The model API key is not configured.')
        return await judge.calibrate(kind)

    app.mount('/assets', StaticFiles(directory=ROOT / 'ui'), name='assets')

    @app.get('/')
    def index():
        return FileResponse(ROOT / 'ui' / 'index.html')

    return app


app = create_app()
