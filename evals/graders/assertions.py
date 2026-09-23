"""Ground-truth assertions with explicit missing-evidence failures."""

from __future__ import annotations

import inspect
import json
import re
import unicodedata
from typing import Any

MISSING = object()
TERMINAL = {"complete", "conditional", "declined", "unclear_final", "unresolved", "skipped_time"}


def strict_equal(actual: Any, expected: Any) -> bool:
    """JSON booleans are not numbers; missing is never JSON null."""
    return type(actual) is type(expected) and actual == expected


def normalize_quote(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value).lower()
    return " ".join("".join(c for c in text if not unicodedata.category(c).startswith("P")).split())


def normalize_numbers(value: str) -> str:
    return re.sub(r"(?<=\d),(?=\d)", "", unicodedata.normalize("NFKC", value)).casefold()


def number_tokens(value: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", normalize_numbers(value)))


def sheet_from(state: dict | None) -> dict:
    if not isinstance(state, dict):
        return {}
    return state.get("answer_sheet", state.get("sheet", state))


def form_from(trace: dict) -> dict:
    form = trace.get("form")
    return form if isinstance(form, dict) else {}


def _json_value(value: Any) -> Any:
    return "<missing>" if value is MISSING else value


def assertion(path, scope, stage, passed, expected, actual, reason=None, **extra) -> dict:
    return {"path": path, "scope": scope, "stage": stage, "passed": bool(passed),
            "reason": reason or ("Matches the expected value." if passed else "Expected evidence was not found."),
            "expected": _json_value(expected), "actual": _json_value(actual), **extra}


def _field_match(item: dict, key: str, expected: Any) -> bool:
    if key.endswith("_in"):
        actual = item.get(key[:-3], MISSING)
        return isinstance(expected, list) and any(strict_equal(actual, val) for val in expected)
    if key == "has_condition":
        value = item.get("condition")
        present = isinstance(value, str) and bool(value.strip())
        return strict_equal(present, expected)
    if key == "value_contains":
        actual = item.get("value")
        return isinstance(actual, str) and str(expected).casefold() in actual.casefold()
    return strict_equal(item.get(key, MISSING), expected)


def item_matches(actual: Any, expected: dict) -> bool:
    return isinstance(actual, dict) and all(_field_match(actual, key, value) for key, value in expected.items())


def _grade_form(expected: dict, form: dict, form_present: bool) -> list[dict]:
    output = []
    for key, value in expected.items():
        path = f"form.{key}"
        if key in {"answers", "questions"}:
            actual = form.get(key if key == "answers" else "candidate_questions", form.get(key, MISSING))
            if not value:
                passed = isinstance(actual, list) and len(actual) == 0
                output.append(assertion(path, "form", "final", passed, [], actual,
                                        "No answers proposed." if passed else "Expected an explicit empty list; the form is missing or contains items."))
                continue
            rows = actual if isinstance(actual, list) else []
            matched_indices = set()
            for index, item in enumerate(value):
                matches = [i for i, candidate in enumerate(rows) if item_matches(candidate, item)]
                matched_indices.update(matches)
                reason = "One proposed item matches every expected field." if matches else "No single proposed item matches all expected fields."
                output.append(assertion(f"{path}[{index}]", "form", "final", bool(matches), item,
                                        rows[matches[0]] if matches else rows, reason))
            extras = [row for i, row in enumerate(rows) if i not in matched_indices]
            if key == "answers" and extras and output:
                output[-1]["warnings"] = ["Additional proposed answers are permitted by this expectation."]
                output[-1]["extra_answers"] = extras
        elif key == "no_answer_for":
            rows = form.get("answers", MISSING)
            forbidden = [a for a in rows if isinstance(a, dict) and a.get("criterion") in value] if isinstance(rows, list) else []
            passed = form_present and isinstance(rows, list) and not forbidden
            output.append(assertion(path, "form", "final", passed, value, forbidden if isinstance(rows, list) else rows,
                                    "No answers target the excluded criteria." if passed else "Excluded criteria were answered, or the form is missing."))
        elif key == "flag":
            actual = form.get(key, MISSING)
            output.append(assertion(path, "form", "final", strict_equal(actual, value), value, actual))
        else:
            output.append(assertion(path, "form", "final", False, value, MISSING, "Unsupported form assertion."))
    return output


def _grade_sheet(expected: dict, state: dict, message: str, prefix="state_after", scope="state_after") -> list[dict]:
    sheet = sheet_from(state)
    output = []
    fields = []
    for key, value in expected.items():
        if isinstance(value, dict) and "." not in key:
            fields.extend((f"{key}.{field}", val) for field, val in value.items())
        else:
            fields.append((key, value))
    for key, value in fields:
        criterion, sep, field = key.partition(".")
        row = sheet.get(criterion, {}) if isinstance(sheet, dict) else {}
        row = row if isinstance(row, dict) else {}
        actual_field = "quote" if field == "quote_in_message" else "value" if field == "value_contains" else field[:-3] if field.endswith("_in") else field
        actual = row.get(actual_field, MISSING)
        if field == "quote_in_message":
            quote = normalize_quote(actual)
            # Empty quotes would otherwise be substrings of every message.
            present = bool(quote) and quote in normalize_quote(message)
            passed = strict_equal(present, value)
            reason = "The saved quote is present in the candidate message." if passed else "The saved quote is missing or is not a normalized substring of the candidate message."
        else:
            passed = bool(sep) and _field_match(row, field, value)
            reason = "Saved state matches." if passed else f"Saved {criterion}.{actual_field} does not match the expectation."
        output.append(assertion(f"{prefix}.{key}", scope, "final", passed, value, actual, reason))
    return output


def _parsed_attempt(attempt: Any) -> dict | None:
    if not isinstance(attempt, dict):
        return None
    for key in ("parsed", "parsed_json", "response"):
        if isinstance(attempt.get(key), dict):
            return attempt[key]
    if "parsed" not in attempt and ("acknowledgement" in attempt or "answer_text" in attempt):
        return attempt
    raw = attempt.get("raw_text", attempt.get("raw"))
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else None
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        return None


def _reply_views(trace: dict) -> list[tuple[str, str, str | None]]:
    views = []
    attempts = trace.get("speak_attempts", [])
    if attempts:
        attempt = attempts[0]
        parsed = _parsed_attempt(attempt)
        ack = parsed.get("acknowledgement") if parsed else None
        text = attempt.get("text", attempt.get("rendered_text")) if isinstance(attempt, dict) else None
        if not isinstance(text, str):
            text = " ".join(str(parsed.get(k, "")) for k in ("acknowledgement", "answer_text")).strip() if parsed else str(attempt.get("raw_text", attempt.get("raw", ""))) if isinstance(attempt, dict) else str(attempt)
        views.append(("first", text, ack if isinstance(ack, str) else None))
    delivered = trace.get("delivered", MISSING)
    ack = trace.get("delivered_acknowledgement", trace.get("acknowledgement"))
    if isinstance(delivered, dict):
        ack = delivered.get("acknowledgement", ack)
        delivered = delivered.get("text", delivered.get("message", MISSING))
    # Path A has no model acknowledgement by design.
    if not attempts and ack is None:
        ack = ""
    views.append(("delivered", delivered if isinstance(delivered, str) else "", ack if isinstance(ack, str) else None))
    return views


async def _model_assert(model_grader, kind, payload, path, scope, stage, expected) -> dict:
    if model_grader is None:
        return assertion(path, scope, stage, False, expected, None,
                         "Model grader unavailable; this assertion was not verified.", grader=kind, calibrated=False, unavailable=True)
    try:
        method = model_grader.grade if hasattr(model_grader, "grade") else model_grader
        result = method(kind, payload)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, dict) or type(result.get("pass")) is not bool or not isinstance(result.get("reason"), str):
            raise ValueError("Invalid grader output")
        return assertion(path, scope, stage, result["pass"], expected, result,
                         result["reason"], grader=kind, calibrated=result.get("calibrated") is True,
                         unavailable=result.get("unavailable") is True)
    except Exception:
        # Provider exception strings may contain request bodies or credentials.
        return assertion(path, scope, stage, False, expected, None,
                         "Model grader failed; this assertion was not verified.", grader=kind, calibrated=False, unavailable=True)


async def _grade_reply(expected: dict, trace: dict, job: dict, model_grader) -> list[dict]:
    output = []
    facts = [f.get("text", "") for f in job.get("job_facts", job.get("facts", [])) if isinstance(f, dict)]
    fact_numbers = number_tokens(" ".join(facts))
    delivery_missing = "delivered" not in trace or (bool(trace.get("agent_error")) and not trace.get("delivered")) or trace.get("delivery_status") in {"failed", "not_delivered", "unavailable"}
    first_attempt = (trace.get("speak_attempts") or [{}])[0]
    first_missing = isinstance(first_attempt, dict) and first_attempt.get("status") == "failed" and not first_attempt.get("raw_text") and not first_attempt.get("parsed")
    for stage, text, ack in _reply_views(trace):
        for key, value in expected.items():
            path = f"reply.{key}"
            lowered = text.casefold()
            actual: Any = text
            reason = None
            if key in {"claims_supported_by_facts", "must_not_claim", "grader_no_praise"}:
                kind = "claims" if key == "claims_supported_by_facts" else "must_not_claim" if key == "must_not_claim" else "praise"
                if (stage == "delivered" and delivery_missing) or (stage == "first" and first_missing):
                    output.append(assertion(path, "reply", stage, False, value, None,
                                            "Reply output is unavailable in the trace.", grader=kind, calibrated=False, unavailable=True))
                    continue
                payload = {"reply": text, "facts": facts} if kind == "claims" else {"reply": text, "forbidden_claims": value} if kind == "must_not_claim" else {"acknowledgement": ack}
                if kind == "praise" and ack is None:
                    output.append(assertion(path, "reply", stage, False, value, None,
                                            "The acknowledgement was not captured.", grader=kind, calibrated=False, unavailable=True))
                else:
                    output.append(await _model_assert(model_grader, kind, payload, path, "reply", stage, value))
                continue
            if key == "must_mention_any":
                passed = any(str(phrase).casefold() in lowered for phrase in value)
            elif key == "must_include_all":
                missing = [phrase for phrase in value if normalize_numbers(str(phrase)) not in normalize_numbers(text)]
                passed = not missing
                reason = "All required phrases are present." if passed else f"Missing required phrases: {', '.join(missing)}."
            elif key == "must_not_contain":
                actual = [phrase for phrase in value if str(phrase).casefold() in lowered]
                passed = not actual
            elif key == "must_not_contain_words":
                actual = [phrase for phrase in value if re.search(r"(?<!\w)" + re.escape(str(phrase)) + r"(?!\w)", text, flags=re.IGNORECASE)]
                passed = not actual
            elif key == "numbers_from_facts":
                actual = sorted(number_tokens(text) - fact_numbers)
                passed = strict_equal(not actual, value)
                reason = "Every numeric token appears in a job fact." if passed else f"Unsupported numbers: {', '.join(actual)}."
            elif key == "no_question_in_ack":
                actual = ack
                passed = ack is not None and strict_equal("?" not in ack, value)
                reason = "Acknowledgement contains no question mark." if passed else "Acknowledgement contains a question mark or was not captured."
            elif key == "question_marks_exactly":
                actual = text.count("?")
                passed = strict_equal(actual, value)
            else:
                passed = False
                reason = "Unsupported reply assertion."
            # Missing delivered evidence must never satisfy negative assertions.
            if (stage == "delivered" and delivery_missing) or (stage == "first" and first_missing):
                passed, reason = False, "Reply output is unavailable in the trace."
            output.append(assertion(path, "reply", stage, passed, value, actual, reason))
    return output


def verdict_value(verdict: Any) -> Any:
    if isinstance(verdict, str):
        return verdict
    if not isinstance(verdict, dict):
        return MISSING
    val = verdict.get("verdict", verdict.get("code_verdict", MISSING))
    return val.get("verdict", MISSING) if isinstance(val, dict) else val


async def grade_case(case: dict, traces: list[dict], final_state: dict, verdict: dict | None,
                     job: dict, model_grader=None) -> list[dict]:
    """Grade every expectation, retaining both model and delivered reply evidence."""
    expected = case.get("expected", {})
    trace = traces[-1] if traces else {}
    form = form_from(trace)
    output = []
    message = case.get("input", "")
    if case.get("run_mode") == "replay":
        message = "\n".join(str(x.get("message", x.get("text", ""))) if isinstance(x, dict) else str(x) for x in case.get("script", []))
    for key, value in expected.items():
        if key == "form":
            output.extend(_grade_form(value, form, isinstance(trace.get("form"), dict)))
        elif key in {"state_after", "final_sheet"}:
            output.extend(_grade_sheet(value, final_state if key == "final_sheet" else trace.get("state_after", final_state),
                                       message, key, "replay" if key == "final_sheet" else "state_after"))
        elif key in {"next_action", "next_action_in"}:
            actual = trace.get("next_action", MISSING)
            passed = any(strict_equal(actual, v) for v in value) if key.endswith("_in") else strict_equal(actual, value)
            output.append(assertion(key, "state_after", "final", passed, value, actual))
        elif key == "reply":
            output.extend(await _grade_reply(value, trace, job, model_grader))
        elif key in {"never_asked", "asked_at_most_once"}:
            verbs = {"ask", "reask", "followup", "simple_reask"} if key == "never_asked" else {"ask", "reask"}
            actions = [t.get("next_action", "") for t in traces]
            setup = case.get("setup", {})
            if case.get("run_mode") == "replay" and setup.get("pending_action") and setup.get("current_criterion"):
                actions.insert(0, f"{setup['pending_action']}:{setup['current_criterion']}")
            for criterion in value:
                actual = [a for a in actions if isinstance(a, str) and a.partition(":")[0] in verbs and a.partition(":")[2] == criterion]
                passed = bool(traces) and (len(actual) == 0 if key == "never_asked" else len(actual) <= 1)
                output.append(assertion(f"{key}.{criterion}", "replay", "final", passed, 0 if key == "never_asked" else "at most 1", actual))
        elif key == "close_reason":
            state = final_state.get("state", final_state)
            actual = final_state.get("close_reason", state.get("close_reason", MISSING) if isinstance(state, dict) else MISSING)
            output.append(assertion(key, "replay", "final", strict_equal(actual, value), value, actual))
        elif key == "all_must_haves_terminal":
            sheet = sheet_from(final_state)
            actual = {c["id"]: sheet.get(c["id"], {}).get("status", "<missing>") for c in job.get("criteria", []) if c.get("must_have")}
            passed = bool(actual) and strict_equal(all(status in TERMINAL for status in actual.values()), value)
            output.append(assertion(key, "replay", "final", passed, value, actual))
        elif key in {"verdict", "verdict_not", "verdict_note_contains"}:
            if key == "verdict_note_contains":
                actual = verdict.get("verdict_note", verdict.get("note", MISSING)) if isinstance(verdict, dict) else MISSING
                passed = isinstance(actual, str) and str(value).casefold() in actual.casefold()
            else:
                actual = verdict_value(verdict)
                passed = strict_equal(actual, value) if key == "verdict" else isinstance(actual, str) and actual in {"fit", "not_fit", "inconclusive"} and not strict_equal(actual, value)
            output.append(assertion(key, "verdict", "final", passed, value, actual))
        elif key == "grader":
            for name, enabled in value.items():
                if name != "value_supported_by_quote" or enabled is not True:
                    output.append(assertion(f"grader.{name}", "grader", "final", False, enabled, MISSING, "Unsupported model assertion."))
                    continue
                sheet = sheet_from(final_state)
                criteria = list(dict.fromkeys(a["criterion"] for a in expected.get("form", {}).get("answers", []) if "criterion" in a))
                if not criteria:
                    criteria = [cid for cid, row in sheet.items() if isinstance(row, dict) and row.get("value")]
                if not criteria:
                    output.append(assertion(f"grader.{name}", "grader", "final", False, True, None, "No saved values were available to grade.", grader="quote", calibrated=False))
                for criterion in criteria:
                    row = sheet.get(criterion, {})
                    payload = {"criterion": criterion, "value": row.get("value"), "quote": row.get("quote")}
                    if not all(isinstance(payload[k], str) and payload[k].strip() for k in ("value", "quote")):
                        output.append(assertion(f"grader.{name}.{criterion}", "grader", "final", False, True, payload,
                                                "The saved value or quote is missing.", grader="quote", calibrated=False))
                        continue
                    output.append(await _model_assert(model_grader, "quote", payload, f"grader.{name}.{criterion}", "grader", "final", True))
        else:
            output.append(assertion(key, "grader", "final", False, value, MISSING, "Unsupported assertion; cannot silently ignore expected ground truth."))
    return output
