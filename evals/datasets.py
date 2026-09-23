"""Immutable dataset registry and strict, readable validation at the boundary."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_DATASET = "hiring-agent-evals-v1"
STATUSES = {"not_asked", "asked", "partial", "unclear", "needs_confirmation", "complete",
            "conditional", "declined", "unclear_final", "unresolved", "skipped_time"}
EXPECTED_KEYS = {"form", "state_after", "next_action", "next_action_in", "reply", "final_sheet",
                 "never_asked", "asked_at_most_once", "close_reason", "all_must_haves_terminal",
                 "verdict", "verdict_not", "verdict_note_contains", "grader"}


class DatasetError(ValueError):
    pass


def content_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _require(condition: bool, location: str, message: str) -> None:
    if not condition:
        raise DatasetError(f"{location}: {message}")


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_expected(expected: dict, criteria: list[str], loc: str):
    def strings(value, path):
        _require(isinstance(value, list) and all(isinstance(x, str) for x in value), path, "must be a list of strings")

    for key, value in expected.items():
        path = loc + ".expected." + key
        if key == "form":
            _require(isinstance(value, dict) and not set(value) - {"answers", "questions", "no_answer_for", "flag"}, path, "invalid form assertions")
            for field, wanted in value.items():
                fpath = path + "." + field
                if field in {"answers", "questions"}:
                    _require(isinstance(wanted, list), fpath, "must be a list")
                    allowed = ({"criterion", "event_in", "status_in", "yes_no", "yes_no_in", "implied", "has_condition", "value_contains", "missing_part"}
                               if field == "answers" else {"type_in", "fact_key", "fact_key_in"})
                    for item in wanted:
                        _require(isinstance(item, dict) and bool(item) and not set(item) - allowed, fpath, "invalid expected item fields")
                        if field == "answers":
                            _require(item.get("criterion") in criteria, fpath, "every expected answer needs a valid criterion")
                        for name, val in item.items():
                            if name.endswith("_in"):
                                _require(isinstance(val, list) and bool(val), fpath + "." + name, "must be a non-empty list")
                            elif name in {"implied", "has_condition"}:
                                _require(isinstance(val, bool), fpath + "." + name, "must be a boolean")
                            else:
                                _require(isinstance(val, str) or val is None and name in {"yes_no", "missing_part"}, fpath + "." + name, "must be text (or a supported null)")
                elif field == "no_answer_for":
                    strings(wanted, fpath)
                    _require(all(x in criteria for x in wanted), fpath, "contains an unknown criterion")
                else:
                    _require(isinstance(wanted, str), fpath, "must be text")
        elif key in {"state_after", "final_sheet"}:
            _require(isinstance(value, dict) and bool(value), path, "must contain field assertions")
            flat = {}
            for name, wanted in value.items():
                if isinstance(wanted, dict):
                    flat.update({name + "." + nested: target for nested, target in wanted.items()})
                else:
                    flat[name] = wanted
            fields = {"status", "status_in", "value", "value_contains", "quote", "quote_in_message", "yes_no",
                      "condition", "obtained_via", "followups_used", "confirmed", "implied", "corrected", "missing_part"}
            for name, wanted in flat.items():
                criterion, separator, field = name.partition(".")
                _require(bool(separator) and criterion in criteria and field in fields, path + "." + name, "unknown criterion or answer field")
                if field == "status_in":
                    strings(wanted, path + "." + name)
                elif field in {"confirmed", "implied", "corrected", "quote_in_message"}:
                    _require(isinstance(wanted, bool), path + "." + name, "must be a boolean")
                elif field == "followups_used":
                    _require(isinstance(wanted, int) and not isinstance(wanted, bool) and wanted >= 0, path + "." + name, "must be a non-negative integer")
        elif key == "reply":
            list_fields = {"must_mention_any", "must_include_all", "must_not_contain", "must_not_contain_words", "must_not_claim"}
            bool_fields = {"numbers_from_facts", "no_question_in_ack", "claims_supported_by_facts", "grader_no_praise"}
            _require(isinstance(value, dict) and bool(value) and not set(value) - (list_fields | bool_fields | {"question_marks_exactly"}), path, "invalid reply assertions")
            for field, wanted in value.items():
                if field in list_fields:
                    strings(wanted, path + "." + field)
                elif field in bool_fields:
                    _require(isinstance(wanted, bool), path + "." + field, "must be a boolean")
                else:
                    _require(isinstance(wanted, int) and not isinstance(wanted, bool) and wanted >= 0, path + "." + field, "must be a non-negative integer")
        elif key in {"never_asked", "asked_at_most_once", "next_action_in"}:
            strings(value, path)
            if key != "next_action_in":
                _require(all(x in criteria for x in value), path, "contains an unknown criterion")
        elif key == "all_must_haves_terminal":
            _require(isinstance(value, bool), path, "must be a boolean")
        elif key == "grader":
            _require(isinstance(value, dict) and set(value) == {"value_supported_by_quote"} and value["value_supported_by_quote"] is True,
                     path, "only value_supported_by_quote: true is supported")
        else:
            _require(isinstance(value, str), path, "must be text")


def validate_dataset(cases: Any, job: Any, taxonomy: Any) -> list[dict[str, Any]]:
    """Reject malformed snapshots before making any model calls or writing results."""
    if isinstance(cases, dict):
        wrapper = cases
        cases = wrapper.get("cases")
        if "case_count" in wrapper:
            _require(isinstance(cases, list) and wrapper["case_count"] == len(cases), "dataset.case_count", "does not match cases")
    _require(isinstance(cases, list) and bool(cases), "dataset.cases", "must be a non-empty list")
    _require(len(cases) <= 10000, "dataset.cases", "maximum is 10,000 cases")
    _require(isinstance(job, dict) and isinstance(job.get("job_id"), str), "job", "job_id is required")
    _require(isinstance(job.get("criteria"), list) and bool(job["criteria"]), "job.criteria", "must be a non-empty list")
    criteria = [c.get("id") for c in job["criteria"] if isinstance(c, dict)]
    _require(len(criteria) == len(job["criteria"]) and all(isinstance(c, str) and c for c in criteria)
             and len(set(criteria)) == len(criteria), "job.criteria", "criterion IDs must be non-empty and unique")
    _require(isinstance(job.get("job_facts"), list), "job.job_facts", "must be a list")
    fact_keys = set()
    for fact in job["job_facts"]:
        _require(isinstance(fact, dict) and isinstance(fact.get("key"), str) and isinstance(fact.get("text"), str), "job.job_facts", "each fact needs key and text strings")
        _require(bool(fact["key"]) and fact["key"] not in fact_keys, "job.job_facts", "fact keys must be non-empty and unique")
        fact_keys.add(fact["key"])
    _require(isinstance(taxonomy, dict) and isinstance(taxonomy.get("failures"), list), "taxonomy", "failures list is required")
    failures = {row.get("code"): row for row in taxonomy["failures"] if isinstance(row, dict)}
    _require(len(failures) == len(taxonomy["failures"]) and all(isinstance(c, str) and c for c in failures),
             "taxonomy.failures", "codes must be non-empty and unique")
    ids = set()
    for index, case in enumerate(cases):
        loc = f"cases[{index}]"
        _require(isinstance(case, dict), loc, "must be an object")
        cid = case.get("id")
        _require(isinstance(cid, str) and bool(cid), loc + ".id", "must be a non-empty string")
        loc = f"case {cid}"
        _require(cid not in ids, loc, "duplicate case ID")
        ids.add(cid)
        for key in ("failure_code", "failure_name", "category", "what_we_test"):
            _require(isinstance(case.get(key), str) and bool(case[key]), loc + "." + key, "is required")
        _require(case["failure_code"] in failures, loc + ".failure_code", "is not in taxonomy")
        _require(case["category"] == failures[case["failure_code"]].get("category"), loc + ".category", "must match taxonomy")
        for key, choices in (("severity", {"minor", "major", "severe"}), ("level", {"turn", "interview"}),
                             ("run_mode", {"turn", "replay", "verdict"}), ("kind", {"trigger", "near_miss"}),
                             ("language", {"en", "hi", "hinglish"})):
            _require(case.get(key) in choices, loc + "." + key, "must be one of " + ", ".join(sorted(choices)))
        _require(isinstance(case.get("runs"), int) and not isinstance(case["runs"], bool) and 1 <= case["runs"] <= 100,
                 loc + ".runs", "must be an integer from 1 to 100")
        _require(isinstance(case.get("tags", {}), (dict, list)), loc + ".tags", "must be an object or list")
        _require(isinstance(case.get("fail_if"), list) and all(isinstance(x, str) for x in case["fail_if"]), loc + ".fail_if", "must be a list of strings")
        setup = case.get("setup")
        _require(isinstance(setup, dict), loc + ".setup", "must be an object")
        _require(setup.get("job") == job["job_id"], loc + ".setup.job", "does not match job_id")
        _require(setup.get("mode") in {"questions", "roleplay", "qa", "closed"}, loc + ".setup.mode", "unknown mode")
        _require(setup.get("current_criterion") is None or setup.get("current_criterion") in criteria, loc + ".setup.current_criterion", "unknown criterion")
        _require(setup.get("pending_action") in {"ask", "confirm", None}, loc + ".setup.pending_action", "must be ask, confirm or null")
        elapsed = setup.get("elapsed_minutes", 0 if case["run_mode"] == "verdict" else None)
        _require(_number(elapsed) and elapsed >= 0, loc + ".setup.elapsed_minutes", "must be a non-negative number")
        streak = setup.get("question_only_streak", 0 if case["run_mode"] == "verdict" else None)
        _require(isinstance(streak, int) and not isinstance(streak, bool) and streak >= 0, loc + ".setup.question_only_streak", "must be a non-negative integer")
        sheet = setup.get("answer_sheet")
        _require(isinstance(sheet, dict) and set(sheet) == set(criteria), loc + ".setup.answer_sheet", "must contain every job criterion exactly once")
        for criterion, row in sheet.items():
            rowloc = loc + ".setup.answer_sheet." + criterion
            _require(isinstance(row, dict) and row.get("status") in STATUSES, rowloc + ".status", "unknown status")
            for field in ("value", "quote"):
                _require(isinstance(row.get(field), str), rowloc + "." + field, "must be a string")
            _require(row["status"] != "complete" or bool(row["quote"].strip()), rowloc, "a complete answer requires a quote")
            _require(row.get("yes_no") in {"yes", "no", None}, rowloc + ".yes_no", "must be yes, no or null")
            _require(row.get("condition") is None or isinstance(row.get("condition"), str), rowloc + ".condition", "must be text or null")
            _require(isinstance(row.get("followups_used"), int) and not isinstance(row["followups_used"], bool) and row["followups_used"] >= 0,
                     rowloc + ".followups_used", "must be a non-negative integer")
            for field in ("confirmed", "implied", "corrected"):
                _require(isinstance(row.get(field), bool), rowloc + "." + field, "must be a boolean")
        for field in ("recent_messages", "hints"):
            _require(isinstance(setup.get(field, [] if case["run_mode"] == "verdict" else None), list), loc + ".setup." + field, "must be a list")
        if case["run_mode"] == "turn":
            _require(isinstance(case.get("input"), str) and bool(case["input"].strip()), loc + ".input", "turn mode requires a message")
        if case["run_mode"] == "replay":
            _require(isinstance(case.get("script"), list) and bool(case["script"]) and all(isinstance(x, str) and x for x in case["script"]), loc + ".script", "replay requires a non-empty message list")
        if case["run_mode"] == "verdict":
            _require(setup["mode"] == "closed", loc + ".setup.mode", "verdict mode requires closed state")
            _require(isinstance(setup.get("close_reason"), str), loc + ".setup.close_reason", "verdict mode requires a close reason")
        for event in case.get("time_events") or []:
            _require(case["run_mode"] == "replay" and isinstance(event, dict), loc + ".time_events", "only replay accepts time events")
            n = event.get("after_turn")
            _require(isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= len(case["script"]), loc + ".time_events.after_turn", "must be a 1-based script turn")
            _require(_number(event.get("advance_minutes")) and event["advance_minutes"] >= 0, loc + ".time_events.advance_minutes", "must be non-negative")
        expected = case.get("expected")
        _require(isinstance(expected, dict) and bool(expected), loc + ".expected", "must contain assertions")
        _require(not (set(expected) - EXPECTED_KEYS), loc + ".expected", "unknown assertion keys: " + ", ".join(sorted(set(expected) - EXPECTED_KEYS)))
        _validate_expected(expected, criteria, loc)
    return copy.deepcopy(cases)


def _bundle(dataset_id: str, name: str, source: Any, taxonomy: dict, job: dict) -> dict:
    cases = validate_dataset(source, job, taxonomy)
    version = str(source.get("version", "1.0")) if isinstance(source, dict) else "1.0"
    value = {"id": dataset_id, "name": name, "version": version, "cases": cases, "taxonomy": taxonomy, "job": job}
    value["hash"] = content_hash({"cases": cases, "taxonomy": taxonomy, "job": job, "version": version})
    value["case_count"] = len(cases)
    value["job_id"] = job["job_id"]
    value["expected_runs"] = sum(c["runs"] for c in cases)
    disputes_path = DATA_DIR / "disputed.json"
    disputes = json.loads(disputes_path.read_text()) if disputes_path.is_file() else []
    if isinstance(disputes, dict):
        disputes = disputes.get("disputes", [])
    _require(isinstance(disputes, list), "disputed.json", "must be a list of dispute records")
    relevant = {}
    for item in disputes:
        _require(isinstance(item, dict) and isinstance(item.get("case_id", item.get("id")), str)
                 and isinstance(item.get("reason"), str) and bool(item["reason"].strip()),
                 "disputed.json", "each dispute requires case_id and a non-empty reason")
        if item.get("dataset_id", DEFAULT_DATASET) == dataset_id:
            relevant[item.get("case_id", item.get("id"))] = item["reason"]
    for case in value["cases"]:
        if case["id"] in relevant:
            case["disputed"] = True
            case["dispute_reason"] = relevant[case["id"]]
    value["disputed_case_count"] = sum(bool(c.get("disputed")) for c in value["cases"])
    value["disputes_hash"] = content_hash(relevant)
    return value


def get_dataset(dataset_id: str | None = None) -> dict:
    dataset_id = dataset_id or DEFAULT_DATASET
    if dataset_id == DEFAULT_DATASET:
        return _bundle(dataset_id, "Customer support · Mumbai", json.loads((DATA_DIR / "cases.json").read_text()),
                       json.loads((DATA_DIR / "taxonomy.json").read_text()),
                       json.loads((DATA_DIR / "jobs/customer_support_mumbai_v1.json").read_text()))
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}", dataset_id):
        raise DatasetError("Invalid dataset ID")
    if os.environ.get("EVAL_DATABASE_URL"):
        from evals.store import Store
        saved = Store().get_document("datasets", dataset_id)
        if saved is None:
            raise DatasetError(f"Unknown dataset: {dataset_id}")
        return _bundle(dataset_id, saved["name"], {"cases": saved["cases"], "version": saved["version"]}, saved["taxonomy"], saved["job"])
    path = DATA_DIR / "imports" / (dataset_id + ".json")
    if not path.is_file():
        raise DatasetError(f"Unknown dataset: {dataset_id}")
    saved = json.loads(path.read_text())
    return _bundle(dataset_id, saved["name"], {"cases": saved["cases"], "version": saved["version"]}, saved["taxonomy"], saved["job"])


def _metadata(bundle: dict) -> dict:
    return {k: v for k, v in bundle.items() if k not in {"cases", "job", "taxonomy"}}


def list_datasets() -> list[dict]:
    rows = [_metadata(get_dataset())]
    if os.environ.get("EVAL_DATABASE_URL"):
        from evals.store import Store
        return rows + [_metadata(get_dataset(name)) for name in Store().list_documents("datasets")]
    for path in sorted((DATA_DIR / "imports").glob("*.json")):
        rows.append(_metadata(get_dataset(path.stem)))
    return rows


def import_dataset(payload: dict) -> dict:
    dataset_id = payload.get("id") or "dataset-" + content_hash(payload)[:12]
    if not isinstance(dataset_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}", dataset_id):
        raise DatasetError("Dataset ID must use letters, numbers, underscores or hyphens")
    if dataset_id == DEFAULT_DATASET:
        raise DatasetError("The bundled source dataset is read-only; use a new dataset ID")
    source = payload.get("cases")
    if isinstance(source, list):
        source = {"cases": source, "version": payload.get("version", "1.0")}
    bundle = _bundle(dataset_id, str(payload.get("name") or dataset_id), source, payload.get("taxonomy"), payload.get("job"))
    if os.environ.get("EVAL_DATABASE_URL"):
        from evals.store import Store
        from psycopg.errors import UniqueViolation
        try:
            Store().save_document("datasets", dataset_id, bundle)
        except UniqueViolation:
            raise DatasetError("Dataset ID already exists; import a new version with a new ID")
        return _metadata(bundle)
    directory = DATA_DIR / "imports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (dataset_id + ".json")
    # Exclusive create: imported datasets are immutable and safe to compare later.
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(bundle, stream, ensure_ascii=False, indent=2, allow_nan=False)
    except FileExistsError:
        raise DatasetError("Dataset ID already exists; import a new version with a new ID")
    return _metadata(bundle)
