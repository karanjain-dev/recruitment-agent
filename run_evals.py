"""Hosted dashboard entry point; one worker owns the evaluation queue."""
import os

if __name__ == '__main__':
    import uvicorn
    uvicorn.run('evals.api:app', host='0.0.0.0', port=int(os.getenv('PORT', '8010')),
                workers=1, proxy_headers=True, forwarded_allow_ips='*')
