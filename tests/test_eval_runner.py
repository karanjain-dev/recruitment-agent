"""Meaningful runner boundaries: immutable fixtures, isolation, failures, persistence."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

from evals.config import resolve_model_config
from evals.datasets import DatasetError, get_dataset, import_dataset, validate_dataset
from evals.runner import execute_run, prepare_run
from evals.store import Store


def test_original_dataset_loads_and_illegal_seed_fails_before_run():
    dataset = get_dataset()
    assert len(dataset["cases"]) == 78
    assert dataset["expected_runs"] == 308
    assert len(dataset["taxonomy"]["failures"]) == 25
    broken = copy.deepcopy(dataset["cases"])
    broken[0]["setup"]["answer_sheet"]["experience"]["quote"] = ""
    with pytest.raises(DatasetError, match="complete answer requires a quote"):
        validate_dataset(broken, dataset["job"], dataset["taxonomy"])
    broken = copy.deepcopy(dataset["cases"])
    broken[0]["expected"]["silently_ignored_assertion"] = True
    with pytest.raises(DatasetError, match="unknown assertion"):
        validate_dataset(broken, dataset["job"], dataset["taxonomy"])


def test_imports_are_immutable_and_path_bounded(tmp_path, monkeypatch):
    import evals.datasets as datasets
    original = get_dataset()
    monkeypatch.setattr(datasets, "DATA_DIR", tmp_path)
    payload = {"id": "new-version", "name": "Future suite", "cases": original["cases"],
               "job": original["job"], "taxonomy": original["taxonomy"]}
    assert import_dataset(payload)["case_count"] == 78
    assert datasets.get_dataset("new-version")["cases"] == original["cases"]
    with pytest.raises(DatasetError, match="already exists"):
        import_dataset(payload)
    with pytest.raises(DatasetError, match="letters"):
        import_dataset(dict(payload, id="../escape"))


def test_disputes_are_annotations_and_do_not_remove_cases(tmp_path, monkeypatch):
    import evals.datasets as datasets
    original = get_dataset()
    monkeypatch.setattr(datasets, "DATA_DIR", tmp_path)
    payload = {"id": "disputed-suite", "cases": original["cases"], "job": original["job"], "taxonomy": original["taxonomy"]}
    import_dataset(payload)
    before = datasets.get_dataset("disputed-suite")
    (tmp_path / "disputed.json").write_text(json.dumps([{"dataset_id": "disputed-suite", "case_id": "READ-01-01", "reason": "Needs human review"}]))
    after = datasets.get_dataset("disputed-suite")
    assert len(after["cases"]) == len(before["cases"]) == 78
    assert before["hash"] == after["hash"]
    assert before["disputes_hash"] != after["disputes_hash"]
    row = next(c for c in after["cases"] if c["id"] == "READ-01-01")
    assert row["disputed"] and row["dispute_reason"] == "Needs human review"


def test_prepare_pins_versions_and_rejects_bad_settings(tmp_path):
    store = Store(tmp_path / "results.db")
    run = prepare_run({"agent_model": "offline-stub", "cases": "READ-01-01", "runs": 2,
                       "prompts": {"speak": "Use a neutral acknowledgement."}}, store)
    assert run["progress"] == {"completed": 0, "total": 2}
    assert run["versions"]["prompt_hashes"]["speak"]
    assert run["versions"]["grader_prompt_hashes"]["claims"]
    assert run["settings"]["prompts"]["speak"] == "Use a neutral acknowledgement."
    assert "api_key" not in json.dumps(run)
    with pytest.raises(ValueError, match="Repeat"):
        prepare_run({"runs": 0}, store)
    with pytest.raises(ValueError, match="No cases"):
        prepare_run({"cases": "DOES-NOT-EXIST"}, store)
    with pytest.raises(ValueError, match="temperature"):
        resolve_model_config("gpt-4.1-mini", temperature=float("nan"))
    assert resolve_model_config("gpt-4.1-mini")["temperature"] == 0
    assert resolve_model_config("gpt-4.1-mini", temperature=None)["temperature"] is None
    defaults = prepare_run({"agent_model": "offline-stub", "cases": "READ-01-01", "runs": 1, "temperature": None}, store)
    assert defaults["versions"]["temperature"] is None
    assert defaults["versions"]["eval_logic_hash"]


def test_real_runtime_stub_repeats_are_fresh_and_database_removed(tmp_path):
    store = Store(tmp_path / "results.db")
    run = prepare_run({"agent_model": "offline-stub", "cases": "READ-01-01", "runs": 2}, store)
    completed = asyncio.run(execute_run(run["id"], store=store))
    results = store.get_results(run["id"])
    assert completed["status"] == "completed"
    assert completed["progress"] == {"completed": 2, "total": 2}
    assert len({r["session_id"] for r in results}) == 2
    setup = run["dataset"]["cases"][0]["setup"]
    for result in results:
        for criterion, expected_row in setup["answer_sheet"].items():
            assert {key: result["seeded_state"]["answer_sheet"][criterion][key] for key in expected_row} == expected_row
        assert result["seeded_state"]["elapsed_minutes"] == setup["elapsed_minutes"]
        assert result["traces"][0]["delivered"]
        assert result["trace_id"] == result["id"]
        assert result["versions"] == run["versions"]
        assert result["passed"]


def test_replay_time_jump_preserves_trace_and_excludes_verdict_cases(tmp_path):
    store = Store(tmp_path / "results.db")
    run = prepare_run({"agent_model": "offline-stub", "cases": "CARRY-03-01,VERDICT-01-02", "runs": 1}, store)
    completed = asyncio.run(execute_run(run["id"], store=store))
    results = store.get_results(run["id"])
    assert completed["status"] == "completed"
    assert all(r["status"] == "failed" for r in results)
    replay = next(r for r in results if r["case_id"] == "CARRY-03-01")
    assert replay["traces"][0]["time_events_after"] == [{"after_turn": 1, "advance_minutes": 2}]
    assert replay["final_state"]["elapsed_minutes"] == 15.5
    assert replay["error"] is None  # Naturally closed; no verdict assertion requires the missing Judge.
    assert len(results) == 1
    assert run['settings']['excluded_case_ids'] == ['VERDICT-01-02']
    assert run['progress']['total'] == 1


def test_crashed_turn_is_failed_result_and_kept_database_is_separate(tmp_path):
    from app.runtime import AgentRuntime

    class CrashingRuntime(AgentRuntime):
        async def handle_turn(self, session_id, message):
            raise RuntimeError("Do not expose provider internals or secrets")

    store = Store(tmp_path / "results.db")
    run = prepare_run({"agent_model": "offline-stub", "cases": "READ-01-01", "runs": 1, "keep_db": True}, store)
    completed = asyncio.run(execute_run(run["id"], store=store, runtime_factory=CrashingRuntime))
    result = store.get_results(run["id"])[0]
    assert completed["status"] == "completed"
    assert result["error"]["code"] == "agent_error"
    assert result["status"] == "failed"
    assert "provider internals" not in json.dumps(result)
    database = Path(completed["test_db_path"])
    assert database.is_file()
    assert database.parent != store.path.parent
    import shutil
    shutil.rmtree(database.parent)


def test_cancellation_and_restart_recovery_preserve_results(tmp_path):
    store = Store(tmp_path / "results.db")
    run = prepare_run({"agent_model": "offline-stub", "cases": "READ-01-01", "runs": 1}, store)
    store.request_cancel(run["id"])
    assert asyncio.run(execute_run(run["id"], store=store))["status"] == "cancelled"
    unfinished = prepare_run({"agent_model": "offline-stub", "cases": "READ-01-01", "runs": 1}, store)
    store.update_run(unfinished["id"], status="running")
    assert Store(store.path).recover_interrupted() == 1
    assert store.get_run(unfinished["id"])["status"] == "interrupted"


def test_comparison_reports_actual_status_changes(tmp_path):
    store = Store(tmp_path / "results.db")
    a = prepare_run({"agent_model": "offline-stub", "cases": "READ-01-01", "runs": 1}, store)
    asyncio.run(execute_run(a["id"], store=store))
    b = prepare_run({"agent_model": "offline-stub", "cases": "READ-01-01", "runs": 1}, store)

    async def fail_grader(*args, **kwargs):
        return [{"path": "controlled_test_failure", "stage": "final", "passed": False, "reason": "Test regression"}]

    asyncio.run(execute_run(b["id"], store=store, grade_fn=fail_grader))
    comparison = store.compare_runs(a["id"], b["id"])
    assert [r["case_id"] for r in comparison["regressions"]] == ["READ-01-01"]
    assert comparison["fixes"] == []
    assert comparison["headline_deltas"]["case_pass_rate"] == -1
    assert comparison["by_failure_code"][0]["delta"] == -1
    assert comparison["slices"]["language"][0]["group"] == "en"


def test_grader_tokens_and_unknown_model_cost_are_honest():
    from evals.runner import _cost_and_usage
    config = {"model": "gpt-4.1-mini", "price_table": {"gpt-4.1-mini": {"input_per_million": .4, "output_per_million": 1.6}}}
    traces = [{"usage": [{"model": "gpt-4.1-mini", "input_tokens": 1000, "output_tokens": 100, "latency_ms": 100}]}]
    assertions = [{"grader": "claims", "actual": {"model_id": "gpt-4.1-mini", "input_tokens": 500, "output_tokens": 50}}]
    usage = _cost_and_usage(traces, None, config, assertions)
    assert usage["input_tokens"] == 1500
    assert usage["output_tokens"] == 150
    assert usage["cost_usd"] == pytest.approx(.00084)
    traces[0]["usage"][0]["model"] = "future-model"
    assert _cost_and_usage(traces, None, config)["cost_usd"] is None
