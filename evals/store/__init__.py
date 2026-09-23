"""Durable eval results, independent from the production interview database."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / "results.sqlite3"


class _PostgresConnection:
    """Keep bound SQL portable while using a dedicated PostgreSQL database."""
    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, values=()):
        return self.connection.execute(query.replace("?", "%s"), values)

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.connection.execute(statement)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class Store:
    def __init__(self, path=None):
        target = str(path or os.environ.get("EVAL_DATABASE_URL") or DEFAULT_PATH)
        self.database_url = target if target.startswith(("postgres://", "postgresql://")) else None
        if self.database_url and self.database_url == os.environ.get("DATABASE_URL"):
            raise ValueError("Evaluation storage must use its own database, separate from the conversation service.")
        self.path = None if self.database_url else Path(target).resolve()
        self._metric_cache = {}
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS eval_runs (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS eval_results (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES eval_runs(id),
                    case_id TEXT NOT NULL, repeat INTEGER NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(run_id, case_id, repeat)
                );
                CREATE INDEX IF NOT EXISTS eval_results_run ON eval_results(run_id,case_id);
                CREATE TABLE IF NOT EXISTS eval_documents (
                    namespace TEXT NOT NULL, name TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(namespace, name)
                );
            """)

    @contextmanager
    def connect(self):
        if self.database_url:
            import psycopg
            from psycopg.rows import dict_row
            with psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=10) as connection:
                yield _PostgresConnection(connection)
            return
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _lock_run(self, db, run_id):
        if self.database_url:
            db.execute("SELECT id FROM eval_runs WHERE id=? FOR UPDATE", (run_id,)).fetchone()
        else:
            db.execute("BEGIN IMMEDIATE")

    def current_metrics(self, run):
        from copy import deepcopy
        from evals.metrics import compute_metrics
        from evals.scope import SCOPE_VERSION
        if (run.get("metrics") and run.get("dataset")
                and run["metrics"].get("accuracy_scope", {}).get("version") != SCOPE_VERSION):
            key = (run["id"], run["updated_at"])
            if key not in self._metric_cache:
                self._metric_cache[key] = compute_metrics(run["dataset"]["cases"], self.get_results(run["id"]),
                                                          expected_repeats=run["settings"].get("runs"))
            run["original_metrics"] = run["metrics"]
            run["metrics"] = deepcopy(self._metric_cache[key])
        return run

    def get_document(self, namespace, name):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM eval_documents WHERE namespace=? AND name=?", (namespace, name)).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_documents(self, namespace):
        with self.connect() as db:
            rows = db.execute("SELECT name,payload FROM eval_documents WHERE namespace=? ORDER BY name", (namespace,)).fetchall()
        return {row["name"]: json.loads(row["payload"]) for row in rows}

    def save_document(self, namespace, name, payload, *, replace=False):
        query = "INSERT INTO eval_documents(namespace,name,payload) VALUES (?,?,?)"
        if replace:
            query += " ON CONFLICT(namespace,name) DO UPDATE SET payload=excluded.payload"
        with self.connect() as db:
            db.execute(query, (namespace, name, _json(payload)))

    def create_run(self, settings: dict, *, versions=None, dataset=None, total=0, run_id=None) -> dict:
        stamp = now()
        run = {"id": run_id or uuid.uuid4().hex[:12], "name": settings.get("name") or "Evaluation run",
               "status": "queued", "created_at": stamp, "updated_at": stamp,
               "settings": settings, "config": settings, "versions": versions or {},
               "progress": {"completed": 0, "total": total}, "metrics": {}, "error": None,
               "diagnostic": settings.get("model_config", {}).get("diagnostic", False),
               "dataset": dataset, "cancel_requested": False}
        with self.connect() as db:
            db.execute("INSERT INTO eval_runs VALUES (?,?,?,?,?)", (run["id"], stamp, stamp, "queued", _json(run)))
        return run

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM eval_runs WHERE id=?", (run_id,)).fetchone()
        return self.current_metrics(json.loads(row["payload"])) if row else None

    def list_runs(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM eval_runs ORDER BY created_at DESC").fetchall()
        runs = []
        for row in rows:
            run = self.current_metrics(json.loads(row["payload"]))
            run.pop("dataset", None)
            # Prompt text stays in detail; lists keep the pinned hashes and IDs.
            for key in ("settings", "config"):
                run[key] = {k: v for k, v in run[key].items() if k != "prompts"}
            runs.append(run)
        return runs

    def update_run(self, run_id: str, **changes) -> dict:
        with self.connect() as db:
            self._lock_run(db, run_id)
            row = db.execute("SELECT payload FROM eval_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError("Unknown run: " + run_id)
            run = json.loads(row["payload"])
            run.update(changes)
            run["updated_at"] = now()
            db.execute("UPDATE eval_runs SET updated_at=?,status=?,payload=? WHERE id=?",
                       (run["updated_at"], run["status"], _json(run), run_id))
        return run

    def save_result(self, run_id: str, result: dict) -> dict:
        result = dict(result)
        result.setdefault("id", uuid.uuid4().hex)
        result["run_id"] = run_id
        result["trace_id"] = result["id"]
        with self.connect() as db:
            self._lock_run(db, run_id)
            db.execute("INSERT INTO eval_results(id,run_id,case_id,repeat,payload) VALUES (?,?,?,?,?)",
                       (result["id"], run_id, result["case_id"], result["repeat"], _json(result)))
            row = db.execute("SELECT payload FROM eval_runs WHERE id=?", (run_id,)).fetchone()
            run = json.loads(row["payload"])
            run["progress"]["completed"] = db.execute("SELECT COUNT(*) AS n FROM eval_results WHERE run_id=?", (run_id,)).fetchone()["n"]
            run["updated_at"] = now()
            db.execute("UPDATE eval_runs SET updated_at=?,payload=? WHERE id=?", (run["updated_at"], _json(run), run_id))
        return result

    def get_results(self, run_id: str, case_id: str | None = None) -> list[dict]:
        query = "SELECT payload FROM eval_results WHERE run_id=?"
        args = [run_id]
        if case_id is not None:
            query += " AND case_id=?"
            args.append(case_id)
        query += " ORDER BY case_id,repeat"
        with self.connect() as db:
            rows = db.execute(query, args).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def get_case_detail(self, run_id: str, case_id: str) -> dict:
        run = self.get_run(run_id)
        if not run:
            raise KeyError("Unknown run: " + run_id)
        case = next((c for c in (run.get("dataset") or {}).get("cases", []) if c["id"] == case_id), None)
        if case is None:
            raise KeyError("Unknown case in this run: " + case_id)
        return {"run": run, "case": case, "results": self.get_results(run_id, case_id)}

    def request_cancel(self, run_id: str) -> dict:
        run = self.get_run(run_id)
        if not run:
            raise KeyError("Unknown run: " + run_id)
        if run["status"] in {"queued", "running", "cancelling"}:
            return self.update_run(run_id, cancel_requested=True, status="cancelling")
        return run

    def recover_interrupted(self) -> int:
        """Call once at app startup, never while workers are active."""
        count = 0
        for run in self.list_runs():
            if run["status"] in {"queued", "running", "cancelling"}:
                full = self.get_run(run["id"])
                saved = self.get_results(run["id"])
                metrics = full.get("metrics", {})
                if full.get("dataset"):
                    from evals.metrics import compute_metrics
                    metrics = compute_metrics(full["dataset"]["cases"], saved, expected_repeats=full["settings"].get("runs"))
                    metrics["grader_calibration"] = full["settings"].get("grader_calibration_snapshot", [])
                self.update_run(run["id"], status="interrupted", finished_at=now(),
                                metrics=metrics, progress={"completed": len(saved), "total": run["progress"]["total"]},
                                error="The evaluation process stopped before completion. Saved traces remain available; start a new run to retry.")
                count += 1
        return count

    def compare_runs(self, left_id: str, right_id: str) -> dict:
        left, right = self.get_run(left_id), self.get_run(right_id)
        if not left or not right:
            raise KeyError("Both comparison runs must exist")
        from evals.metrics import compute_metrics
        lm = left.get("metrics") or compute_metrics(left["dataset"]["cases"], self.get_results(left_id), expected_repeats=left["settings"].get("runs"))
        rm = right.get("metrics") or compute_metrics(right["dataset"]["cases"], self.get_results(right_id), expected_repeats=right["settings"].get("runs"))
        def index(metrics):
            rows = metrics.get("cases", [])
            return rows if isinstance(rows, dict) else {row.get("case_id", row.get("id")): row for row in rows}
        li, ri = index(lm), index(rm)
        cases = {c["id"]: c for c in left["dataset"]["cases"]}
        cases.update({c["id"]: c for c in right["dataset"]["cases"]})
        def passed(row):
            if "passed" in row:
                return row["passed"]
            return row.get("status") in {"pass", "passed"}
        changes, regressions, fixes = [], [], []
        for cid in sorted(set(li) & set(ri)):
            a, b = li[cid], ri[cid]
            if a.get("status") in {"pending", "incomplete"} or b.get("status") in {"pending", "incomplete"}:
                continue
            if passed(a) == passed(b):
                continue
            row = {"case_id": cid, "case": cases[cid], "severity": cases[cid]["severity"],
                   "title": cases[cid].get("title", cases[cid]["what_we_test"]), "before": a, "after": b}
            changes.append(row)
            (regressions if passed(a) else fixes).append(row)
        sort_key = lambda row: ({"severe": 0, "major": 1, "minor": 2}.get(row["severity"], 3), row["case_id"])
        regressions.sort(key=sort_key)
        fixes.sort(key=sort_key)
        lh, rh = lm.get("headline", {}), rm.get("headline", {})
        deltas = {key: rh[key] - value for key, value in lh.items()
                  if isinstance(value, (int, float)) and not isinstance(value, bool)
                  and isinstance(rh.get(key), (int, float)) and not isinstance(rh[key], bool)}
        warnings = []
        for field, label in (("dataset_hash", "Datasets differ"), ("job_hash", "Job configs differ"),
                             ("grader_hash", "Grader configuration changed"),
                             ("eval_logic_hash", "Evaluation logic changed"),
                             ("disputes_hash", "Dispute annotations changed")):
            if left["versions"].get(field) != right["versions"].get(field):
                warnings.append(label + "; interpret the comparison with care.")
        if set(li) != set(ri):
            warnings.append("Case selections differ; changes include only shared completed cases.")
        if left.get("diagnostic") or right.get("diagnostic"):
            warnings.append("Offline diagnostic results do not measure a live model's quality.")
        def group_deltas(aa, bb):
            aa = {row.get("value", row.get("group")): row for row in aa}
            bb = {row.get("value", row.get("group")): row for row in bb}
            rows = []
            for key in sorted(set(aa) | set(bb)):
                av, bv = aa.get(key), bb.get(key)
                ar, br = (av or {}).get("score"), (bv or {}).get("score")
                rows.append({"group": key, "a": av, "b": bv,
                             "delta": br - ar if isinstance(ar, (int, float)) and isinstance(br, (int, float)) else None})
            return rows
        dimensions = sorted(set(lm.get("slices", {})) | set(rm.get("slices", {})))
        return {"a": left, "b": right, "left": left, "right": right,
                "headline": {"a": lh, "b": rh}, "headline_deltas": deltas, "regressions": regressions, "fixes": fixes,
                "changes": changes, "warnings": warnings,
                "by_failure_code": group_deltas(lm.get("by_failure_code", []), rm.get("by_failure_code", [])),
                "slices": {dim: group_deltas(lm.get("slices", {}).get(dim, []), rm.get("slices", {}).get(dim, [])) for dim in dimensions}}
