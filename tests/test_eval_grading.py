"""Hand-counted evaluation semantics; no model calls or production database."""

import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from evals.graders import ModelGrader, grade_case
from evals.metrics import compare_metrics, compute_metrics


def grade(expected, *, form=None, state=None, trace=None, mode="turn", model_grader=None, job=None, verdict=None):
    case = {"id": "TEST-01", "run_mode": mode, "input": "I live in Malad!", "expected": expected}
    tr = {"form": form if form is not None else {"answers": [], "candidate_questions": [], "flag": "none"},
          "state_after": state or {}, "delivered": "Thanks.", "speak_attempts": []}
    tr.update(trace or {})
    return asyncio.run(grade_case(case, [tr], state or {}, verdict, job or {}, model_grader))


def test_expected_answer_fields_must_match_the_same_item():
    expected = {"form": {"answers": [{"criterion": "location", "event_in": ["answer"], "status_in": ["complete"]}]}}
    rows = [{"criterion": "location", "event": "answer", "status": "partial"},
            {"criterion": "location", "event": "volunteered", "status": "complete"}]
    assertions = grade(expected, form={"answers": rows})
    assert assertions[0]["passed"] is False
    rows[0]["status"] = "complete"
    assertions = grade(expected, form={"answers": rows})
    assert assertions[0]["passed"] is True
    assert assertions[0]["extra_answers"] == [rows[1]]


def test_missing_answers_is_not_an_explicit_empty_answer_list():
    assert not grade({"form": {"answers": []}}, form={})[0]["passed"]
    assert grade({"form": {"answers": []}}, form={"answers": []})[0]["passed"]


def test_nullable_match_is_not_missing_and_booleans_are_not_numbers():
    expected = {"form": {"answers": [{"criterion": "night_shifts", "yes_no_in": [None]}]}}
    assert grade(expected, form={"answers": [{"criterion": "night_shifts", "yes_no": None}]})[0]["passed"]
    assert not grade(expected, form={"answers": [{"criterion": "night_shifts"}]})[0]["passed"]
    for actual in [0, "false", None]:
        assert not grade({"state_after": {"night_shifts.confirmed": False}}, state={"answer_sheet": {"night_shifts": {"confirmed": actual}}})[0]["passed"]


def test_quote_is_unicode_normalized_nonempty_and_source_grounded():
    expected = {"state_after": {"location.quote_in_message": True}}
    assert grade(expected, state={"answer_sheet": {"location": {"quote": "I LIVE  in Malad."}}})[0]["passed"]
    assert not grade(expected, state={"answer_sheet": {"location": {"quote": ""}}})[0]["passed"]
    assert not grade(expected, state={"answer_sheet": {"location": {"quote": "I live in Powai"}}})[0]["passed"]


def test_reply_first_attempt_and_delivery_are_independent():
    result = grade({"reply": {"must_not_contain_words": ["great", "well done"], "question_marks_exactly": 1, "no_question_in_ack": True}},
                   trace={"speak_attempts": [{"parsed": {"acknowledgement": "Great!", "answer_text": ""}, "text": "Great! Where do you live?"}],
                          "delivered": "Thanks. Where do you live?", "delivered_acknowledgement": "Thanks."})
    assert len(result) == 6
    assert not result[0]["passed"] and result[0]["stage"] == "first"
    assert all(a["passed"] for a in result if a["stage"] == "delivered")
    assert grade({"reply": {"must_not_contain_words": ["pass"]}}, trace={"delivered": "Please bypass that question."})[0]["passed"]


def test_number_normalization_and_numeric_fact_boundaries():
    job = {"job_facts": [{"text": "₹20,000 to ₹30,000; 10 PM to 7 AM."}]}
    result = grade({"reply": {"must_include_all": ["20,000", "30,000"], "numbers_from_facts": True}},
                   trace={"delivered": "₹20000 to 30,000."}, job=job)
    assert all(a["passed"] for a in result)
    result = grade({"reply": {"numbers_from_facts": True}}, trace={"delivered": "The amount is 300."}, job=job)
    assert not result[0]["passed"]
    assert result[0]["actual"] == ["300"]


def test_missing_model_grader_fails_honestly():
    result = grade({"reply": {"claims_supported_by_facts": True}})
    assert not result[0]["passed"]
    assert result[0]["unavailable"] and not result[0]["calibrated"]


def test_no_question_and_no_praise_use_only_acknowledgement():
    calls = []
    async def model(kind, payload):
        calls.append((kind, payload))
        return {"pass": True, "reason": "Neutral acknowledgement.", "calibrated": False}
    result = grade({"reply": {"no_question_in_ack": True, "grader_no_praise": True}},
                   trace={"delivered": "Thanks. Do you work nights?", "delivered_acknowledgement": "Thanks."}, model_grader=model)
    assert all(a["passed"] for a in result)
    assert calls == [("praise", {"acknowledgement": "Thanks."})]


def test_replay_counting_uses_exact_action_vocabulary():
    case = {"run_mode": "replay", "expected": {"asked_at_most_once": ["location"], "never_asked": ["notice_period"]}}
    traces = [{"next_action": action} for action in ["ask:location", "followup:location", "simple_reask:location", "confirm:notice_period"]]
    results = asyncio.run(grade_case(case, traces, {}, None, {}))
    assert all(r["passed"] for r in results)
    traces.append({"next_action": "reask:location"})
    traces.append({"next_action": "followup:notice_period"})
    assert not any(r["passed"] for r in asyncio.run(grade_case(case, traces, {}, None, {})))


def test_missing_verdict_cannot_pass_a_negative_assertion():
    assert not grade({"verdict_not": "not_fit"}, mode="verdict")[0]["passed"]
    assert not grade({"verdict_not": "not_fit"}, mode="verdict", verdict={"verdict": None})[0]["passed"]
    assert grade({"verdict_not": "not_fit"}, mode="verdict", verdict={"code_verdict": "inconclusive"})[0]["passed"]


def test_failed_model_output_cannot_pass_negative_reply_checks():
    rows = grade({"reply": {"must_not_contain_words": ["excellent"], "no_question_in_ack": True}},
                 trace={"agent_error": {"code": "model_error"}, "delivered": "", "delivery_status": "not_delivered",
                        "speak_attempts": [{"status": "failed", "raw_text": None, "parsed": None, "acknowledgement": ""}],
                        "delivered_acknowledgement": ""})
    assert len(rows) == 4 and not any(row["passed"] for row in rows)


def test_replay_counts_seeded_initial_question():
    case = {"run_mode": "replay", "setup": {"current_criterion": "experience", "pending_action": "ask"},
            "expected": {"asked_at_most_once": ["experience"]}}
    rows = asyncio.run(grade_case(case, [{"next_action": "reask:experience"}], {}, None, {}))
    assert rows[0]["actual"] == ["ask:experience", "reask:experience"]
    assert not rows[0]["passed"]


def fixture_cases():
    return [{"id": cid, "severity": severity, "runs": 3, "failure_code": "READ-01", "failure_name": "Read", "category": "READ",
             "language": lang, "kind": "trigger", "level": "turn", "tags": ["cause"],
             "expected": {"form": {"flag": "none", "answers": [{"criterion": "night_shifts", "yes_no": yn, "status_in": ["complete"]}], "questions": [{"fact_key": fact}]}}}
            for cid, severity, lang, yn, fact in [("S", "severe", "en", "no", "salary"), ("M", "moderate", "hi", "yes", "unknown")]]


def fixture_results():
    results = []
    for cid, outcomes in [("S", ["passed", "passed", "failed"]), ("M", ["passed", "passed", "failed"])]:
        for i, status in enumerate(outcomes):
            form = {"flag": "none" if i < 2 else "underage", "answers": [{"criterion": "night_shifts", "yes_no": "no" if cid == "S" else "yes", "status": "complete"}],
                    "questions": [{"fact_key": "salary" if cid == "S" else "unknown"}]}
            results.append({"case_id": cid, "repeat": i, "status": status, "traces": [{"form": form}],
                            "assertions": [{"scope": "reply", "stage": "first", "path": "reply.praise", "passed": status == "passed"},
                                           {"scope": "reply", "stage": "delivered", "path": "reply.praise", "passed": status == "passed" or cid == "S"}],
                            "latency_ms": 100 * (i + 1), "input_tokens": 10, "output_tokens": 5, "cost_usd": .01})
    return results


def test_metrics_hand_count_severity_flakiness_classifiers_and_slices():
    metrics = compute_metrics(fixture_cases(), fixture_results())
    h = metrics["headline"]
    assert h["severe_failures"] == 1 and h["passed_cases"] == 1 and h["flaky_cases"] == 2
    assert h["case_pass_rate"] == .5 and h["run_pass_rate"] == 4 / 6 and h["assertion_accuracy"] == 9 / 12
    assert h["input_tokens"] == 60 and h["cost_usd"] == pytest.approx(.06)
    assert metrics["reply_rates"][0]["model_failure_rate"] == 2 / 6
    assert metrics["reply_rates"][0]["product_failure_rate"] == 1 / 6
    flag = next(c for c in metrics["classifiers"] if c["classifier"] == "flag_detection" and c["positive_class"] == "none")
    assert flag["tp"] == 4 and flag["fn"] == 2 and flag["precision"] == 1 and flag["recall"] == 4 / 6
    assert flag["f1"] == .8
    assert {c["classifier"] for c in metrics["classifiers"]} == {"flag_detection", "knockout_refusal", "complete_detection", "fact_matching"}
    assert metrics["slices"]["language"][0]["headline"]["case_pass_rate"] == 0
    assert all("classifiers" in r and "reply_rates" in r for r in metrics["slices"]["kind"])


def test_partial_repeats_are_never_reported_as_passing_cases():
    metrics = compute_metrics(fixture_cases(), fixture_results()[:1])
    assert metrics["headline"]["completed_cases"] == 0
    assert metrics["headline"]["case_pass_rate"] is None
    assert metrics["cases"][0]["status"] == "incomplete"
    metrics = compute_metrics(fixture_cases()[:1], fixture_results()[:1], expected_repeats=1)
    assert metrics["cases"][0]["status"] == "passed"


def test_ambiguous_classification_expectations_are_excluded():
    cases = fixture_cases()[:1]
    cases[0]["expected"]["form"] = {"answers": [{"criterion": "night_shifts", "yes_no_in": ["no", None], "status_in": ["complete", "unclear"]}], "questions": [{"fact_key_in": ["salary", "unknown"]}]}
    metrics = compute_metrics(cases, fixture_results()[:3])
    assert all(row["support"] == 0 for row in metrics["classifiers"])


def test_tag_keys_get_their_own_value_slices_and_missing_cost_stays_unknown():
    cases = fixture_cases()
    cases[0]["tags"] = {"cause": "invented facts", "phrasing": "leading"}
    cases[1]["tags"] = {"cause": "lookup error", "phrasing": "plain"}
    results = fixture_results()
    results[0].update(cost_usd=None, cost_known_usd=.003, error={"code": "agent_error"}, status="failed")
    metrics = compute_metrics(cases, results)
    assert {row["value"] for row in metrics["slices"]["tag:cause"]} == {"invented facts", "lookup error"}
    assert {row["value"] for row in metrics["slices"]["tags"]} == {"cause: invented facts", "cause: lookup error", "phrasing: leading", "phrasing: plain"}
    assert metrics["headline"]["error_runs"] == 1
    assert metrics["headline"]["cost_usd"] is None
    assert metrics["headline"]["known_cost_usd"] == pytest.approx(.053)


def test_disputed_cases_stay_in_all_scores_and_are_exposed_separately():
    cases = fixture_cases()
    cases[0].update(disputed=True, dispute_reason="Reviewer questions this expectation.")
    metrics = compute_metrics(cases, fixture_results())
    assert metrics["headline"]["disputed_cases"] == 1
    assert metrics["headline"]["total_cases"] == 2
    assert metrics["headline"]["case_pass_rate"] == .5
    assert metrics["headline"]["severe_failures"] == 1
    assert [case["case_id"] for case in metrics["disputed_cases"]] == ["S"]
    assert metrics["disputed_cases"][0]["dispute_reason"] == "Reviewer questions this expectation."
    assert metrics["slices"]["language"][0]["headline"]["disputed_cases"] == 1


def test_compare_captures_every_changed_completed_case():
    before = compute_metrics(fixture_cases(), fixture_results())
    results = deepcopy(fixture_results())
    for result in results:
        result["status"] = "passed" if result["case_id"] == "S" else "failed"
    after = compute_metrics(fixture_cases(), results)
    comparison = compare_metrics(before, after)
    assert [r["case_id"] for r in comparison["regressions"]] == ["M"]
    assert [r["case_id"] for r in comparison["fixes"]] == ["S"]


def test_model_grader_is_fixed_strict_and_does_not_leak_provider_errors(tmp_path):
    seen = []
    async def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"pass": True, "reason": "Neutral."})}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            grader = ModelGrader({"model_id": "fixed-grader", "temperature": 0}, client=client, api_key="not-real", calibration_path=tmp_path)
            result = await grader.grade("praise", {"acknowledgement": "Thanks."})
            assert result["pass"] and not result["calibrated"]
            assert result["input_tokens"] == 10
    asyncio.run(run())
    assert seen[0]["model"] == "fixed-grader" and seen[0]["temperature"] == 0
    assert seen[0]["response_format"]["json_schema"]["strict"] is True
    with pytest.raises(ValueError):
        ModelGrader({"temperature": .1})


def test_calibration_rejects_draft_labels_and_requires_human_review(tmp_path):
    data = {"label_source": "ai_draft", "reviewed_by": None, "examples": [{"id": i, "input": {"acknowledgement": "Thanks."}, "human_label": True, "reviewed": True} for i in range(30)]}
    (tmp_path / "praise.json").write_text(json.dumps(data))
    grader = ModelGrader({"model_id": "fixed"}, calibration_path=tmp_path)
    report = asyncio.run(grader.calibrate("praise"))
    assert report["reviewed_examples"] == 0 and not report["calibrated"]
    data.update(label_source="human_reviewed", reviewed_by="Human evaluator")
    (tmp_path / "praise.json").write_text(json.dumps(data))
    async def grade_stub(kind, payload):
        return {"pass": True, "reason": "Neutral.", "calibrated": False}
    grader.grade = grade_stub
    report = asyncio.run(grader.calibrate("praise"))
    assert report["agreement"] == 1 and report["calibrated"]
    assert next(row for row in grader.calibration_summary() if row["grader"] == "praise")["calibrated"]
    # Changing the grader invalidates the old agreement report.
    changed = ModelGrader({"model_id": "different"}, calibration_path=tmp_path)
    assert not next(row for row in changed.calibration_summary() if row["grader"] == "praise")["calibrated"]
