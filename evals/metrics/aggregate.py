"""Metric denominators are explicit; incomplete cases never become passes."""

from __future__ import annotations

from collections import defaultdict
import math
from typing import Any

from evals.graders.assertions import form_from
from evals.scope import accuracy_view

DIMENSIONS = ("category", "failure_code", "severity", "language", "kind", "level", "tags")
FLAGS = ("none", "underage", "distress", "wrong_person", "abuse", "manipulation", "identity_question")


def rate(numerator: int | float, denominator: int | float) -> float | None:
    return numerator / denominator if denominator else None


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _numeric(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


def _is_error(result: dict) -> bool:
    return result.get("status") == "error" or bool(result.get("error")) or any(a.get("path") == "agent_error" for a in result.get("assertions", []))


def _known_cost(result: dict) -> float:
    return result["cost_usd"] if _numeric(result.get("cost_usd")) else result.get("cost_known_usd", 0) if _numeric(result.get("cost_known_usd")) else 0


def _case_summary(case: dict, results: list[dict], expected_repeats=None) -> dict:
    expected = expected_repeats.get(case["id"], case.get("runs", 1)) if isinstance(expected_repeats, dict) else expected_repeats if isinstance(expected_repeats, int) else case.get("runs", 1)
    expected = max(1, expected)
    passed = sum(r.get("status") == "passed" for r in results)
    failed = sum(r.get("status") in {"failed", "error"} for r in results)
    errors = sum(_is_error(r) for r in results)
    completed = len(results) >= expected
    severe = case.get("severity", "").casefold() == "severe"
    passes = completed and (passed == len(results) if severe else passed > len(results) / 2)
    status = "passed" if passes else "failed" if completed else "pending" if not results else "incomplete"
    assertions = [a for r in results for a in r.get("assertions", [])]
    failing = next((a for a in assertions if not a.get("passed")), None)
    latencies = [r["latency_ms"] for r in results if _numeric(r.get("latency_ms"))]
    costs = [r["cost_usd"] for r in results if _numeric(r.get("cost_usd"))]
    return {"case_id": case["id"], "failure_code": case.get("failure_code"), "failure_name": case.get("failure_name"),
            "title": case.get("what_we_test", case.get("failure_name", case["id"])),
            **{key: case.get(key, [] if key == "tags" else "unknown") for key in DIMENSIONS},
            "status": status, "passed": passes, "completed": completed, "expected_runs": expected,
            "runs": len(results), "passed_runs": passed, "failed_runs": failed, "error_runs": errors,
            "pass_rate": rate(passed, len(results)), "flaky": bool(passed and failed),
            "first_failure": failing, "assertions": len(assertions),
            "assertions_passed": sum(a.get("passed") is True for a in assertions),
            "assertion_accuracy": rate(sum(a.get("passed") is True for a in assertions), len(assertions)),
            "latency_p50_ms": percentile(latencies, .5), "latency_p95_ms": percentile(latencies, .95),
            "cost_usd": sum(costs) if len(costs) == len(results) and results else None,
            "known_cost_usd": sum(_known_cost(r) for r in results), "cost_per_run_usd": rate(sum(costs), len(results)) if len(costs) == len(results) else None,
            "disputed": case.get("disputed", False), "dispute_reason": case.get("dispute_reason")}


def _binary_row(name: str, positive: str, pairs: list[tuple[str, str]], labels: list[str] | None = None) -> dict:
    label_set = list(dict.fromkeys((labels or []) + [value for pair in pairs for value in pair]))
    matrix = {expected: {actual: 0 for actual in label_set} for expected in label_set}
    for expected, actual in pairs:
        matrix[expected][actual] += 1
    tp = sum(e == positive and a == positive for e, a in pairs)
    fp = sum(e != positive and a == positive for e, a in pairs)
    fn = sum(e == positive and a != positive for e, a in pairs)
    tn = sum(e != positive and a != positive and a != "<missing>" for e, a in pairs)
    precision = rate(tp, tp + fp)
    recall = rate(tp, tp + fn)
    f1 = rate(2 * tp, 2 * tp + fp + fn)
    return {"classifier": name, "positive_class": positive, "precision": precision, "recall": recall,
            "f1": f1, "support": len(pairs), "positive_support": sum(e == positive for e, _ in pairs),
            "accuracy": rate(sum(e == a for e, a in pairs), len(pairs)),
            "missing_predictions": sum(a == "<missing>" for _, a in pairs),
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "binary_confusion_matrix": {"labels": [positive, "other"], "values": [[tp, fn], [fp, tn]],
                                        "orientation": "rows=expected, columns=actual; missing negative predictions reported separately"},
            "confusion_matrix": {"labels": label_set, "rows": matrix,
                                 "values": [[matrix[e][a] for a in label_set] for e in label_set],
                                 "orientation": "rows=expected, columns=actual"}}


def classifiers(cases: list[dict], results: list[dict]) -> list[dict]:
    by_id = {c["id"]: c for c in cases}
    pairs = {"flag_detection": [], "knockout_refusal": [], "complete_detection": [], "fact_matching": []}
    for result in results:
        case = by_id.get(result.get("case_id"))
        if not case:
            continue
        expected = case.get("expected", {}).get("form", {})
        traces = result.get("traces", [])
        actual = form_from(traces[-1]) if traces else {}
        if isinstance(expected.get("flag"), str):
            pairs["flag_detection"].append((expected["flag"], str(actual.get("flag", "<missing>"))))
        answers = actual.get("answers", [])
        for answer in expected.get("answers", []):
            candidate = next((a for a in answers if isinstance(a, dict) and a.get("criterion") == answer.get("criterion")), {})
            if answer.get("criterion") == "night_shifts" and "yes_no" in answer and answer["yes_no"] in {"yes", "no", None}:
                prediction = candidate.get("yes_no")
                pairs["knockout_refusal"].append(("no" if answer["yes_no"] == "no" else "not_no", "no" if prediction == "no" else "not_no" if "yes_no" in candidate and prediction in {"yes", None} else "<missing>"))
            status = answer.get("status_in")
            if isinstance(status, list) and len(status) == 1:
                prediction = candidate.get("status")
                pairs["complete_detection"].append(("complete" if status[0] == "complete" else "not_complete", "complete" if prediction == "complete" else "not_complete" if isinstance(prediction, str) else "<missing>"))
        questions = actual.get("candidate_questions", actual.get("questions", []))
        for index, question in enumerate(expected.get("questions", [])):
            if not isinstance(question.get("fact_key"), str):
                continue
            # Pair by question order, never by a predicted label that could hide errors.
            candidate = questions[index] if isinstance(questions, list) and index < len(questions) and isinstance(questions[index], dict) else {}
            predicted = candidate.get("fact_key")
            pairs["fact_matching"].append(("known" if question["fact_key"] != "unknown" else "unknown", "<missing>" if not isinstance(predicted, str) else "unknown" if predicted == "unknown" else "known"))
    flags = list(dict.fromkeys(list(FLAGS) + [e for e, _ in pairs["flag_detection"]]))
    rows = [_binary_row("flag_detection", flag, pairs["flag_detection"], flags) for flag in flags]
    rows.extend([_binary_row("knockout_refusal", "no", pairs["knockout_refusal"], ["no", "not_no"]),
                 _binary_row("complete_detection", "complete", pairs["complete_detection"], ["complete", "not_complete"]),
                 _binary_row("fact_matching", "known", pairs["fact_matching"], ["known", "unknown"])])
    return rows


def reply_rates(results: list[dict]) -> list[dict]:
    grouped = defaultdict(lambda: {"first": [], "delivered": []})
    for result in results:
        for a in result.get("assertions", []):
            if a.get("scope") == "reply" and a.get("stage") in {"first", "delivered"}:
                grouped[a.get("path", "reply")][a["stage"]].append(a)
    rows = []
    for path, stages in sorted(grouped.items()):
        first, delivered = stages["first"], stages["delivered"]
        model_failures = sum(a.get("passed") is not True for a in first)
        product_failures = sum(a.get("passed") is not True for a in delivered)
        model_rate, product_rate = rate(model_failures, len(first)), rate(product_failures, len(delivered))
        rows.append({"assertion": path, "model_total": len(first), "model_failures": model_failures,
                     "model_failure_rate": model_rate, "product_total": len(delivered), "product_failures": product_failures,
                     "product_failure_rate": product_rate, "checker_gap": model_rate - product_rate if model_rate is not None and product_rate is not None else None,
                     "unavailable": sum(a.get("unavailable") is True for a in first + delivered)})
    return rows


def _core(cases: list[dict], results: list[dict], summaries: list[dict]) -> dict:
    all_assertions = [a for r in results for a in r.get("assertions", [])]
    passed_assertions = sum(a.get("passed") is True for a in all_assertions)
    code_assertions = [a for a in all_assertions if not a.get("grader")]
    form_assertions = [a for a in code_assertions if a.get("scope") == "form"]
    complete = [c for c in summaries if c["completed"]]
    passed_cases = sum(c["passed"] for c in complete)
    costs = [r["cost_usd"] for r in results if _numeric(r.get("cost_usd"))]
    latency = [r["latency_ms"] for r in results if _numeric(r.get("latency_ms"))]
    totals = {"total_cases": len(cases), "completed_cases": len(complete), "passed_cases": passed_cases,
              "disputed_cases": sum(bool(c.get("disputed")) for c in summaries),
              "failed_cases": sum(c["status"] == "failed" for c in summaries),
              "pending_cases": sum(c["status"] in {"pending", "incomplete"} for c in summaries),
              "flaky_cases": sum(c["flaky"] for c in summaries),
              "severe_failures": sum(c["severity"] == "severe" and c["status"] == "failed" for c in summaries),
              "severe_pending": sum(c["severity"] == "severe" and not c["completed"] for c in summaries),
              "case_pass_rate": rate(passed_cases, len(complete)), "run_count": len(results),
              "passed_runs": sum(r.get("status") == "passed" for r in results),
              "failed_runs": sum(r.get("status") in {"failed", "error"} for r in results),
              "error_runs": sum(_is_error(r) for r in results),
              "run_pass_rate": rate(sum(r.get("status") == "passed" for r in results), len(results)),
              "assertion_count": len(all_assertions), "passed_assertions": passed_assertions,
              "assertion_accuracy": rate(passed_assertions, len(all_assertions)),
              "code_assertion_count": len(code_assertions),
              "code_assertions_passed": sum(a.get("passed") is True for a in code_assertions),
              "code_assertion_accuracy": rate(sum(a.get("passed") is True for a in code_assertions), len(code_assertions)),
              "form_assertion_count": len(form_assertions),
              "form_assertions_passed": sum(a.get("passed") is True for a in form_assertions),
              "form_assertion_accuracy": rate(sum(a.get("passed") is True for a in form_assertions), len(form_assertions)),
              "cost_usd": sum(costs) if len(costs) == len(results) and results else None,
              "known_cost_usd": sum(_known_cost(r) for r in results), "cost_coverage": rate(len(costs), len(results)),
              "latency_p50_ms": percentile(latency, .5), "latency_p95_ms": percentile(latency, .95),
              "input_tokens": sum(r.get("input_tokens", 0) or 0 for r in results),
              "output_tokens": sum(r.get("output_tokens", 0) or 0 for r in results)}
    speed = {"latency_p50_ms": totals["latency_p50_ms"], "latency_p95_ms": totals["latency_p95_ms"],
             "cost_usd": totals["cost_usd"], "known_cost_usd": totals["known_cost_usd"],
             "cost_per_run_usd": rate(sum(costs), len(results)) if len(costs) == len(results) else None,
             "cost_per_case_usd": rate(sum(costs), len({r.get("case_id") for r in results})) if len(costs) == len(results) else None,
             "input_tokens": totals["input_tokens"], "output_tokens": totals["output_tokens"]}
    return {"headline": totals, "classifiers": classifiers(cases, results), "reply_rates": reply_rates(results), "cost_speed": speed}


def compute_metrics(cases: list[dict], results: list[dict], expected_repeats=None) -> dict:
    cases, results, scope = accuracy_view(cases, results)
    by_case = defaultdict(list)
    for result in results:
        by_case[result.get("case_id")].append(result)
    summaries = [_case_summary(case, by_case[case["id"]], expected_repeats) for case in cases]
    output = _core(cases, results, summaries)
    output["accuracy_scope"] = scope
    output["cases"] = summaries
    # Disputes annotate evidence; they never remove cases from any denominator.
    output["disputed_cases"] = [summary for summary in summaries if summary.get("disputed")]
    output["slices"] = {}
    tag_keys = sorted({key for case in cases if isinstance(case.get("tags"), dict) for key in case["tags"]})
    for dimension in (*DIMENSIONS, *(f"tag:{key}" for key in tag_keys)):
        groups = defaultdict(list)
        for case in cases:
            tags = case.get("tags", [])
            if dimension.startswith("tag:"):
                values = [tags.get(dimension[4:], "untagged")] if isinstance(tags, dict) else ["untagged"]
            elif dimension == "tags":
                values = [f"{key}: {value}" for key, value in tags.items()] if isinstance(tags, dict) else tags
            else:
                values = [case.get(dimension, "unknown")]
            for value in values or ["untagged"]:
                groups[str(value)].append(case)
        rows = []
        for value, selected in sorted(groups.items()):
            ids = {c["id"] for c in selected}
            selected_summaries = [c for c in summaries if c["case_id"] in ids]
            selected_results = [r for r in results if r.get("case_id") in ids]
            core = _core(selected, selected_results, selected_summaries)
            rows.append({"value": value, "label": value, "cases": len(selected),
                         "passed": core["headline"]["passed_cases"], "failed": core["headline"]["failed_cases"],
                         "flaky": core["headline"]["flaky_cases"], "pending": core["headline"]["pending_cases"],
                         "score": core["headline"]["case_pass_rate"], **core})
        output["slices"][dimension] = rows
    output["by_category"] = output["slices"]["category"]
    output["by_failure_code"] = output["slices"]["failure_code"]
    graders = defaultdict(list)
    for result in results:
        for a in result.get("assertions", []):
            if a.get("grader"):
                graders[a["grader"]].append(a)
    output["grader_calibration"] = [{"grader": name, "calibrated": all(a.get("calibrated") is True for a in rows),
                                     "assertions": len(rows), "unavailable": sum(a.get("unavailable") is True for a in rows)}
                                    for name, rows in sorted(graders.items())]
    output["has_uncalibrated_graders"] = any(not row["calibrated"] for row in output["grader_calibration"])
    return output


def compare_metrics(a: dict, b: dict) -> dict:
    """Compare only completed cases appearing in both runs."""
    by_a = {c["case_id"]: c for c in a.get("cases", [])}
    by_b = {c["case_id"]: c for c in b.get("cases", [])}
    comparable = {cid for cid in by_a.keys() & by_b.keys() if by_a[cid].get("completed") and by_b[cid].get("completed")}
    changes = [{"case_id": cid, "severity": by_b[cid].get("severity"), "a": by_a[cid], "b": by_b[cid]} for cid in comparable if by_a[cid]["status"] != by_b[cid]["status"]]
    changes.sort(key=lambda c: (c["severity"] != "severe", c["case_id"]))
    def delta(left, right):
        return {key: right.get(key) - left.get(key) for key in left.keys() & right.keys() if _numeric(left.get(key)) and _numeric(right.get(key))}
    slice_deltas = {}
    for dimension in sorted(a.get("slices", {}).keys() | b.get("slices", {}).keys()):
        left = {row["value"]: row for row in a.get("slices", {}).get(dimension, [])}
        right = {row["value"]: row for row in b.get("slices", {}).get(dimension, [])}
        slice_deltas[dimension] = [{"value": value, "delta": delta(left[value]["headline"], right[value]["headline"])} for value in sorted(left.keys() & right.keys())]
    return {"headline_deltas": delta(a.get("headline", {}), b.get("headline", {})),
            "regressions": [c for c in changes if c["a"]["status"] == "passed"],
            "fixes": [c for c in changes if c["b"]["status"] == "passed"],
            "changes": changes, "comparable_cases": len(comparable), "slice_deltas": slice_deltas,
            "by_failure_code": slice_deltas.get("failure_code", []),
            "unmatched_cases": sorted(by_a.keys() ^ by_b.keys())}
