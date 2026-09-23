"""Idempotently seed the dedicated hosted database with the recorded baseline."""
import gzip
import json
from pathlib import Path

ROOT = Path(__file__).parent


def baseline_report():
    path = ROOT / 'baseline.json'
    return json.loads(path.read_text()) if path.is_file() else None


def seed_baseline(store):
    report = baseline_report()
    if not report:
        return
    source = json.loads(gzip.decompress((ROOT / (report['id'] + '.json.gz')).read_bytes()))
    run = source['run']
    with store.connect() as db:
        if not db.execute('SELECT id FROM eval_runs WHERE id=?', (run['id'],)).fetchone():
            db.execute('INSERT INTO eval_runs VALUES (?,?,?,?,?)',
                       (run['id'], run['created_at'], run['updated_at'], run['status'], json.dumps(run)))
            for result in source['results']:
                db.execute('INSERT INTO eval_results(id,run_id,case_id,repeat,payload) VALUES (?,?,?,?,?)',
                           (result['id'], run['id'], result['case_id'], result['repeat'], json.dumps(result)))
    store.save_document('release', 'baseline', report, replace=True)
