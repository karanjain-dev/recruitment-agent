"""Serve the studio and eval workspace together, with separate databases."""
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.main import app as studio, health as studio_health
from evals.api import app as evaluations


@asynccontextmanager
async def lifespan(app):
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(studio.router.lifespan_context(studio))
        await stack.enter_async_context(evaluations.router.lifespan_context(evaluations))
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get('/health')
def health():
    status = studio_health()
    if isinstance(status, JSONResponse):
        return status
    try:
        with evaluations.state.store.connect() as db:
            db.execute('SELECT 1').fetchone()
    except Exception:
        return JSONResponse({'status': 'unhealthy', 'eval_database': 'unavailable'}, status_code=503)
    return {**status, 'eval_database': 'connected'}


app.mount('/evals', evaluations)
app.mount('/', studio)
