"""Conversation-only accuracy, without rewriting historical evidence."""
from copy import deepcopy

SCOPE_VERSION = "conversation-v1"
VERDICT_KEYS = {"verdict", "verdict_not", "verdict_note_contains"}


def conversation_case(case):
    value = deepcopy(case)
    value["expected"] = {k: v for k, v in case.get("expected", {}).items() if k not in VERDICT_KEYS}
    return value


def accuracy_view(cases, results):
    excluded_ids = {c["id"] for c in cases if c.get("run_mode") == "verdict"}
    selected = [conversation_case(c) for c in cases if c["id"] not in excluded_ids]
    ids = {c["id"] for c in selected}
    scoped, removed = [], 0
    for source in results:
        if source.get("case_id") not in ids:
            removed += len(source.get("assertions", []))
            continue
        result = deepcopy(source)
        assertions = []
        for a in result.get("assertions", []):
            missing_judge = (a.get("path") == "agent_error" and isinstance(a.get("actual"), dict)
                             and a["actual"].get("cause") == "missing_verdict_hook")
            if a.get("scope") == "verdict" or a.get("path") in VERDICT_KEYS or missing_judge:
                removed += 1
            else:
                assertions.append(a)
        error = result.get("error")
        if isinstance(error, dict) and error.get("cause") == "missing_verdict_hook":
            result["error"] = None
        if len(assertions) != len(result.get("assertions", [])) or result.get("error") != error:
            result["assertions"] = assertions
            result["passed"] = bool(assertions) and all(a.get("passed") is True for a in assertions) and not result.get("error")
            result["status"] = "passed" if result["passed"] else "error" if result.get("error") else "failed"
        scoped.append(result)
    return selected, scoped, {"version": SCOPE_VERSION, "name": "Conversation accuracy",
                              "excluded_case_ids": sorted(excluded_ids), "excluded_cases": len(excluded_ids),
                              "excluded_assertions": removed, "source_case_count": len(cases),
                              "reason": "Final hiring verdicts are outside conversation accuracy."}
