"""Command-line interface for local evaluation workflows."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from evals.config import get_grader_config
from evals.runner import _api_key, execute_run, prepare_run
from evals.store import Store


def parser():
    root = argparse.ArgumentParser(prog="python -m evals", description="Local screening-agent evaluation workbench")
    sub = root.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run a frozen dataset against an agent configuration")
    run.add_argument("--agent-model", default="gpt-4.1-mini")
    run.add_argument("--dataset", dest="dataset_id")
    run.add_argument("--cases", help="Case glob, failure code, or comma-separated selections")
    run.add_argument("--runs", type=int, help="Override repeats for each case")
    run.add_argument("--name")
    run.add_argument("--understand-model")
    run.add_argument("--speak-model")
    run.add_argument("--temperature", type=float)
    run.add_argument("--understand-prompt", help="UTF-8 prompt text file")
    run.add_argument("--speak-prompt", help="UTF-8 prompt text file")
    run.add_argument("--keep-db", action="store_true")
    run.add_argument("--store", help="Alternative results SQLite path")
    compare = sub.add_parser("compare", help="Compare two saved runs")
    compare.add_argument("run_a")
    compare.add_argument("run_b")
    compare.add_argument("--store")
    calibrate = sub.add_parser("calibrate", help="Measure a fixed grader against reviewed human labels")
    calibrate.add_argument("--grader", required=True, choices=["claims", "praise", "quote", "must_not_claim"])
    calibrate.add_argument("--labels", help="Optional reviewed calibration file")
    ui = sub.add_parser("ui", help="Open the local evaluation dashboard server")
    ui.add_argument("--port", type=int, default=8010)
    return root


async def _run(args):
    from pathlib import Path
    store = Store(args.store)
    settings = {key: value for key, value in vars(args).items()
                if key not in {"command", "store", "understand_prompt", "speak_prompt"} and value is not None}
    settings["prompts"] = {kind: Path(getattr(args, kind + "_prompt")).read_text()
                           for kind in ("understand", "speak") if getattr(args, kind + "_prompt")}
    run = prepare_run(settings, store)
    print(f"Run {run['id']}: {run['progress']['total']} attempts · {run['settings']['agent_model']}", flush=True)
    if run.get("diagnostic"):
        print("Offline diagnostic only: this fixture does not measure live model accuracy.", flush=True)
    result = await execute_run(run["id"], store=store)
    print(json.dumps({"id": result["id"], "status": result["status"], "progress": result["progress"],
                      "headline": result.get("metrics", {}).get("headline", {}), "error": result.get("error"),
                      "test_db_path": result.get("test_db_path")}, indent=2))
    return 0 if result["status"] == "completed" else 1


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "run":
            return asyncio.run(_run(args))
        if args.command == "compare":
            data = Store(args.store).compare_runs(args.run_a, args.run_b)
            print(json.dumps({key: data[key] for key in ("headline_deltas", "regressions", "fixes", "warnings")}, indent=2))
            return 0
        if args.command == "calibrate":
            from evals.graders import ModelGrader
            grader = ModelGrader(get_grader_config(), api_key=_api_key())
            report = asyncio.run(grader.calibrate(args.grader, args.labels))
            print(json.dumps(report, indent=2))
            return 0
        if args.command == "ui":
            import uvicorn
            print(f"Evaluation dashboard: http://127.0.0.1:{args.port}", flush=True)
            uvicorn.run("evals.api:app", host="127.0.0.1", port=args.port)
            return 0
    except (ValueError, KeyError, OSError) as exc:
        print("Evaluation error: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
