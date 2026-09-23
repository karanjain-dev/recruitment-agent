"""Run frozen cases against fresh, isolated agent sessions and preserve evidence."""
from __future__ import annotations

import asyncio
import copy
import fnmatch
import hashlib
import inspect
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from evals.config import (PROJECT_ROOT, get_grader_config, list_model_configs,
                          resolve_model_config, resolve_prompts)
from evals.datasets import content_hash, get_dataset, validate_dataset
from evals.store import Store, now
from evals.scope import SCOPE_VERSION, conversation_case


def _git_commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT,
                                       stderr=subprocess.DEVNULL, text=True, timeout=3).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def select_cases(cases, selector=None):
    if not selector:
        return cases
    patterns = selector if isinstance(selector, list) else [x.strip() for x in str(selector).split(",") if x.strip()]
    matched = [c for c in cases if any(fnmatch.fnmatchcase(c["id"], p) or c["failure_code"] == p for p in patterns)]
    if not matched:
        raise ValueError("No cases match this selection")
    return matched


def prepare_run(settings: dict, store: Store | None = None) -> dict:
    """Validate all inputs and freeze versions before a background task starts."""
    store = store or Store()
    if not isinstance(settings, dict):
        raise ValueError("Run settings must be an object")
    dataset = get_dataset(settings.get("dataset_id"))
    selected = select_cases(dataset["cases"], settings.get("cases"))
    excluded_ids = [c["id"] for c in selected if c["run_mode"] == "verdict"]
    selected = [c for c in selected if c["run_mode"] != "verdict"]
    if not selected:
        raise ValueError("Final-verdict cases are excluded. Select conversation cases to evaluate.")
    repeats = settings.get("runs")
    if repeats is not None and (not isinstance(repeats, int) or isinstance(repeats, bool) or not 1 <= repeats <= 100):
        raise ValueError("Repeat override must be an integer from 1 to 100")
    model_name = settings.get("agent_model") or settings.get("model_config_name") or "gpt-4.1-mini"
    model_overrides = {key: settings[key] for key in ("understand_model", "speak_model", "temperature") if key in settings}
    config = resolve_model_config(model_name, **model_overrides)
    config["price_table"] = {row["model"]: row["pricing"] for row in list_model_configs() if row.get("pricing")}
    prompts = resolve_prompts(settings.get("prompts"))
    grader_config = get_grader_config()
    from evals.graders import ModelGrader
    from evals.graders.model import BASE_PROMPT, PROMPTS
    calibration_snapshot = ModelGrader(grader_config, api_key="").calibration_summary()
    grader_prompt_hashes = {key: hashlib.sha256((BASE_PROMPT + value).encode()).hexdigest()
                            for key, value in PROMPTS.items()}
    name = settings.get("name") or ("Offline diagnostic" if config.get("diagnostic") else model_name + " evaluation")
    if not isinstance(name, str) or not name.strip() or len(name) > 160:
        raise ValueError("Run name must contain 1 to 160 characters")
    frozen_settings = {"name": name.strip(), "agent_model": model_name, "dataset_id": dataset["id"],
                       "cases": settings.get("cases"), "case_ids": [c["id"] for c in selected], "runs": repeats,
                       "understand_model": config["understand_model"], "speak_model": config["speak_model"],
                       "temperature": config.get("temperature"), "model_config": config,
                       "prompts": prompts, "grader_config": grader_config,
                       "grader_calibration_snapshot": calibration_snapshot,
                       "accuracy_scope": SCOPE_VERSION, "excluded_case_ids": excluded_ids,
                       "keep_db": bool(settings.get("keep_db", False))}
    source_files = [PROJECT_ROOT / "app" / name for name in ("engine.py", "model.py", "runtime.py", "main.py")]
    source_files += [PROJECT_ROOT / "config/job.json"]
    source_files += [PROJECT_ROOT / "prompts" / name for name in ("roleplay_classify.md", "roleplay_reply.md")]
    source_hashes = {str(path.relative_to(PROJECT_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in source_files if path.is_file()}
    eval_files = [PROJECT_ROOT / "evals" / path for path in
                  ("graders/assertions.py", "graders/model.py", "metrics/aggregate.py", "runner/__init__.py", "datasets.py")]
    eval_source_hashes = {str(path.relative_to(PROJECT_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in eval_files if path.is_file()}
    versions = {"agent_model_config": model_name, "model_id": config["model"],
                "understand_model": config["understand_model"], "speak_model": config["speak_model"],
                "temperature": config.get("temperature"),
                "prompt_hashes": {key: hashlib.sha256(text.encode()).hexdigest() for key, text in prompts.items()},
                "job_id": dataset["job"]["job_id"], "job_version": dataset["job"].get("version", dataset["job"]["job_id"]),
                "job_hash": content_hash(dataset["job"]), "dataset_version": dataset["version"],
                "dataset_hash": dataset["hash"], "taxonomy_hash": content_hash(dataset["taxonomy"]),
                "disputes_hash": dataset.get("disputes_hash"),
                "git_commit": _git_commit(), "agent_source_hashes": source_hashes,
                "eval_source_hashes": eval_source_hashes, "eval_logic_hash": content_hash(eval_source_hashes),
                "grader_model_id": grader_config.get("model_id", grader_config.get("model")),
                "grader_hash": content_hash({"config": grader_config, "prompts": grader_prompt_hashes}),
                "grader_prompt_hashes": grader_prompt_hashes,
                "grader_calibration_hash": content_hash(calibration_snapshot)}
    frozen_dataset = copy.deepcopy(dataset)
    frozen_dataset["cases"] = selected
    total = sum(repeats or case["runs"] for case in selected)
    return store.create_run(frozen_settings, versions=versions, dataset=frozen_dataset, total=total)


def _api_key():
    """Read local credentials without modifying process model configuration."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key
    try:
        from dotenv import dotenv_values
        return (dotenv_values(PROJECT_ROOT / ".env").get("OPENAI_API_KEY") or "").strip()
    except ImportError:
        return ""


def _agent_error(error, trace=None):
    if isinstance(error, dict):
        message = error.get("message") or error.get("cause") or "The agent failed."
    else:
        message = "The agent failed: " + type(error).__name__
    return {"path": "agent_error", "scope": "agent", "stage": "final", "passed": False,
            "reason": message, "expected": "A complete agent result", "actual": error if isinstance(error, dict) else type(error).__name__}


def _cost_and_usage(traces, verdict, config, assertions=None):
    calls = [call for trace in traces for call in trace.get("usage", [])]
    calls += (verdict or {}).get("usage", [])
    grader_calls = []
    for assertion in assertions or []:
        actual = assertion.get("actual")
        if assertion.get("grader") and isinstance(actual, dict) and actual.get("model_id"):
            grader_calls.append({"call": "grader:" + assertion["grader"], "model": actual["model_id"],
                                 "input_tokens": actual.get("input_tokens", 0),
                                 "output_tokens": actual.get("output_tokens", 0)})
    calls += grader_calls
    input_tokens = sum(call.get("input_tokens", call.get("prompt_tokens", 0)) or 0 for call in calls)
    output_tokens = sum(call.get("output_tokens", call.get("completion_tokens", 0)) or 0 for call in calls)
    latency = sum(call.get("latency_ms", call.get("duration_ms", 0)) or 0 for call in calls)
    prices = config.get("price_table", {})
    cost = 0.0
    unknown = []
    for call in calls:
        model = call.get("model") or config.get("model")
        # Provider responses can return a dated revision of a named configuration.
        pricing = prices.get(model)
        if pricing is None:
            pricing = next((price for key, price in prices.items() if str(model).startswith(key + "-")), None)
        if config.get("diagnostic"):
            pricing = {"input_per_million": 0, "output_per_million": 0}
        if pricing is None:
            unknown.append(model)
        else:
            cost += ((call.get("input_tokens", 0) or 0) * pricing["input_per_million"]
                     + (call.get("output_tokens", 0) or 0) * pricing["output_per_million"]) / 1_000_000
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens,
            "model_latency_ms": latency, "cost_usd": None if unknown else cost,
            "cost_known_usd": cost, "unpriced_models": sorted(set(unknown)), "usage": calls}


async def execute_run(run_id: str, store: Store | None = None, runtime_factory=None, grader=None,
                      grade_fn=None, metrics_fn=None) -> dict:
    """Execute a pre-created run. Every repeat is committed before the next starts.

    Dependency injection makes the lifecycle, isolation, and failure paths testable
    without invoking a live provider. No global app store or environment is changed.
    """
    from evals.graders import ModelGrader, grade_case
    from evals.metrics import compute_metrics
    if runtime_factory is None:
        from app.runtime import AgentRuntime
        runtime_factory = AgentRuntime
    store = store or Store()
    run = store.get_run(run_id)
    if not run:
        raise KeyError("Unknown run: " + run_id)
    if run["status"] not in {"queued", "cancelling"}:
        raise ValueError("Only a queued run can be started; create a new run to retry")
    if run.get("cancel_requested"):
        return store.update_run(run_id, status="cancelled", finished_at=now())
    settings, dataset = run["settings"], run["dataset"]
    validate_dataset(dataset["cases"], dataset["job"], dataset["taxonomy"])
    grade_fn = grade_fn or grade_case
    metrics_fn = metrics_fn or compute_metrics
    config = copy.deepcopy(settings["model_config"])
    config["api_key"] = _api_key()
    if grader is None and not config.get("diagnostic"):
        grader_config = dict(settings["grader_config"], calibration_snapshot=settings.get("grader_calibration_snapshot", []))
        grader = ModelGrader(grader_config, api_key=config["api_key"])
    directory = Path(tempfile.mkdtemp(prefix="hiring-eval-" + run_id + "-"))
    db_path = directory / "sessions.sqlite3"
    store.update_run(run_id, status="running", started_at=now(),
                     test_db_path=str(db_path) if settings["keep_db"] else None)
    runtime = None
    all_results = []
    final_status = "completed"
    try:
        for case in dataset["cases"]:
            for repeat in range(1, (settings.get("runs") or case["runs"]) + 1):
                if store.get_run(run_id).get("cancel_requested"):
                    final_status = "cancelled"
                    break
                started = time.perf_counter()
                traces, assertions, verdict, final_state = [], [], None, {}
                seeded_state, session_id, agent_failure = None, None, None
                try:
                    if runtime is None:
                        runtime = runtime_factory(job=copy.deepcopy(dataset["job"]), db_path=db_path,
                                                  model_config=config, prompts=settings["prompts"], stub=bool(config.get("diagnostic")))
                    session_id = runtime.seed(copy.deepcopy(case["setup"]))
                    seeded_state = runtime.snapshot(session_id)
                    if case["run_mode"] == "verdict":
                        verdict = await runtime.close_and_score(session_id)
                        agent_failure = verdict.get("agent_error") or verdict.get("error")
                    else:
                        messages = [case["input"]] if case["run_mode"] == "turn" else case["script"]
                        for turn_index, message in enumerate(messages, 1):
                            trace = await runtime.handle_turn(session_id, message)
                            trace["turn_index"] = turn_index
                            traces.append(trace)
                            agent_failure = trace.get("agent_error") or trace.get("error")
                            for event in case.get("time_events") or []:
                                if event["after_turn"] == turn_index:
                                    runtime.clock.advance_minutes(event["advance_minutes"])
                                    trace.setdefault("time_events_after", []).append(copy.deepcopy(event))
                            state = runtime.snapshot(session_id)
                            if agent_failure or state.get("state") == "closed" or state.get("mode") == "closed":
                                break
                        # Conversation replays never invoke a hiring Judge.
                        # State/close assertions catch incomplete conversations.
                    final_state = runtime.snapshot(session_id)
                except Exception as exc:
                    agent_failure = {"code": "agent_error", "cause": type(exc).__name__,
                                     "message": "The agent could not finish this attempt (" + type(exc).__name__ + ")."}
                    if session_id:
                        try:
                            final_state = runtime.snapshot(session_id)
                        except Exception:
                            pass
                try:
                    assertions = await grade_fn(conversation_case(case), traces, final_state, verdict, dataset["job"],
                                               model_grader=grader if traces or not agent_failure else None)
                except Exception as exc:
                    assertions = [{"path": "grader_error", "scope": "grading", "stage": "final", "passed": False,
                                   "reason": "Grading failed (" + type(exc).__name__ + "); inspect the saved trace.",
                                   "expected": "Every expected assertion is graded", "actual": type(exc).__name__}]
                if agent_failure:
                    assertions.append(_agent_error(agent_failure))
                passed = bool(assertions) and all(a.get("passed") is True for a in assertions)
                initial_question = (["ask:" + case["setup"]["current_criterion"]]
                                    if case["run_mode"] == "replay" and case["setup"].get("pending_action") == "ask"
                                    and case["setup"].get("current_criterion") else [])
                result = {"case_id": case["id"], "repeat": repeat, "status": "passed" if passed else "failed",
                          "passed": passed, "assertions": assertions, "traces": traces,
                          "final_state": final_state, "verdict": verdict, "seeded_state": seeded_state,
                          "session_id": session_id, "error": agent_failure, "versions": run["versions"],
                          "diagnostic": bool(config.get("diagnostic")), "created_at": now(),
                          "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                          "question_sequence": initial_question + [t.get("next_action") for t in traces
                                                if str(t.get("next_action", "")).split(":")[0] in {"ask", "reask", "followup", "simple_reask", "confirm"}]}
                result.update(_cost_and_usage(traces, verdict, config, assertions))
                saved = store.save_result(run_id, result)
                all_results.append(saved)
                metrics = metrics_fn(dataset["cases"], all_results, expected_repeats=settings.get("runs"))
                if grader and hasattr(grader, "calibration_summary"):
                    metrics["grader_calibration"] = grader.calibration_summary()
                store.update_run(run_id, metrics=metrics,
                                 progress={"completed": len(all_results), "total": run["progress"]["total"]})
            if final_status == "cancelled":
                break
    except asyncio.CancelledError:
        store.update_run(run_id, status="interrupted", finished_at=now(), error="The worker stopped. Completed attempts were saved.")
        raise
    except Exception as exc:
        # Infrastructure errors preserve already completed results and never imply
        # the selected dataset successfully completed.
        final_status = "failed"
        store.update_run(run_id, error="Evaluation stopped (" + type(exc).__name__ + "). Completed attempts were saved.")
    finally:
        if runtime is not None:
            if hasattr(runtime, "aclose"):
                await runtime.aclose()
            else:
                closed = runtime.close()
                if inspect.isawaitable(closed):
                    await closed
        if not settings["keep_db"]:
            shutil.rmtree(directory, ignore_errors=True)
    current = store.get_run(run_id)
    if current["status"] == "interrupted":
        return current
    return store.update_run(run_id, status=final_status, finished_at=now())
