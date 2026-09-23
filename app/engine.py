"""Deterministic interview policy. Models propose; these pure functions decide.

No network, database writes, hiring scores, or verdicts belong in this module.
``decide`` copies its inputs and returns a proposed delivery transaction. The
caller persists ``pre_delivery_sheet`` immediately and commits ``sheet`` and
``state`` only after the browser acknowledges the delivered reply.
"""

from __future__ import annotations

from copy import deepcopy
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
from string import Formatter
import unicodedata


MODEL_STATUSES = {"complete", "partial", "unclear", "conditional", "declined"}
EVENTS = {"answer", "volunteered", "correction", "late_answer"}
RESOLVED = {"complete", "conditional", "declined", "unclear_final", "unresolved", "skipped_time"}
ANSWERED = {"complete", "partial", "unclear", "conditional", "declined", "needs_confirmation", "unclear_final", "unresolved"}
# Understand owns classification. These routes never reclassify candidate text.
FLAG_ROUTES = {
    "underage": {"action": "close", "line": "close_underage"},
    "distress": {"action": "close", "line": "close_distress"},
    "wrong_person": {"action": "close", "line": "close_wrong_person"},
    "abuse": {"action": "warn", "line": "abuse_warning", "close_after": 2,
              "close_line": "close_abuse"},
    "manipulation": {"action": "continue", "line": "manipulation_reply"},
    "identity_question": {"action": "continue", "line": "identity_disclosure"},
}
FLAGS = set(FLAG_ROUTES)
UNANSWERED_TURN_LIMIT = 3
QUESTION_TYPES = {"job", "process", "clarify", "outcome", "assessment_hint"}


def load_job() -> dict:
    """Load the approved, versioned job data and reject ambiguous identifiers."""
    path = Path(__file__).resolve().parent.parent / "config" / "job.json"
    job = json.loads(path.read_text(encoding="utf-8"))
    for collection in ("criteria", "facts"):
        key = "id" if collection == "criteria" else "key"
        ids = [entry[key] for entry in job[collection]]
        if not all(isinstance(i, str) and i.strip() for i in ids) or len(set(ids)) != len(ids):
            raise ValueError(f"{collection} must have unique nonempty {key} values")
    orders = [c["order"] for c in job["criteria"]]
    if len(set(orders)) != len(orders):
        raise ValueError("Criterion order values must be unique")
    if not any(c["must_have"] for c in job["criteria"]):
        raise ValueError("At least one must-have criterion is required")
    for criterion in job["criteria"]:
        if criterion["is_knockout"] and criterion.get("confirm_implied_text"):
            raise ValueError("Knockouts cannot have implied confirmation wording")
        if criterion["is_knockout"] and not all(criterion.get(k) for k in ("confirm_text", "confirm_volunteered_text")):
            raise ValueError("Knockouts require neutral confirmation wording")
    return job


def _ordered(job: dict) -> list[dict]:
    return sorted(job["criteria"], key=lambda c: c["order"])


def initial_state(job: dict, now: float) -> dict:
    first = next(c for c in _ordered(job) if c["must_have"])
    return {
        "state": "open", "mode": "screening", "current_criterion_id": first["id"],
        "pending_action": "ask", "last_question_asked": first["question_text"],
        "started_at": now, "deadline_at": now + job.get("duration_seconds", 900),
        "last_candidate_msg_at": now, "counters": {"abuse_count": 0, "question_only_streak": 0,
                                                   "unanswered_streak": 0},
        "roleplay_turns": 0, "roleplay_breaks": 0, "roleplay_done": False,
        "roleplay_last_line": None, "callback_time": None, "paused_at": None,
        "close_reason": None, "seen_flags": [], "confirmations_asked": [],
        "interrupted_questions": {},
    }


def initial_sheet(job: dict) -> dict:
    first_id = next(c["id"] for c in _ordered(job) if c["must_have"])
    return {
        c["id"]: {
            "criterion_id": c["id"], "status": "asked" if c["id"] == first_id else "not_asked",
            "value": None, "quote": None, "yes_no": None, "condition": None,
            "obtained_via": None, "implied": False, "followups_used": 0,
            "corrected": False, "confirmed": False, "missing_part": None,
            "message_id": None,
        }
        for c in _ordered(job)
    }


def build_context(job, state, sheet, hints, candidate_messages, latest) -> dict:
    """The complete allowlist for Understand: never include saved answers."""
    current_id = state.get("current_criterion_id")
    current = next((c for c in job["criteria"] if c["id"] == current_id), None)
    messages = []
    for msg in candidate_messages:
        if isinstance(msg, str):
            messages.append(msg)
        elif isinstance(msg, dict) and msg.get("role", "candidate") in {"candidate", "user"}:
            messages.append(str(msg.get("content", msg.get("text", ""))))
    if messages and messages[-1] == latest:
        messages.pop()
    return {
        "current_criterion": ({k: deepcopy(current[k]) for k in ("id", "name", "complete_when", "examples", "is_yes_no")} if current else None),
        "confirming": str(state.get("resume_action") if state.get("state") == "paused_callback" else state.get("pending_action", "")).startswith("confirm"),
        "allowed_missing_parts": list(current.get("missing_parts", {})) if current else [],
        "other_criteria": [{"id": c["id"], "about": c["about"], "status": sheet[c["id"]]["status"]} for c in _ordered(job) if c["id"] != current_id],
        "hints": [h["quote"] for h in hints if h.get("criterion_id") == current_id and not h.get("resolved")],
        "job_fact_topics": [{"key": f["key"], "topic": f["topic"]} for f in job["facts"]],
        "candidate_messages": messages[-6:], "latest_message": latest,
    }


def _string(value, default="") -> str:
    return value.strip() if isinstance(value, str) else default


def _render_question(template, row) -> str | None:
    """Only substitute supported fields backed by a concrete saved answer."""
    if not isinstance(template, str) or not template.strip():
        return None
    try:
        fields = list(Formatter().parse(template))
        if any(field is not None and (field != "value" or spec or conversion)
               for _, field, spec, conversion in fields):
            return None
        if any(field is not None for _, field, _, _ in fields):
            if (not row or not _string(row.get("value")) or row.get("implied")
                    or row.get("missing_part") or row.get("status") not in {"complete", "needs_confirmation"}):
                return None
        return template.format(value=(row or {}).get("value", ""))
    except (ValueError, KeyError, IndexError):
        return None


def normalize(form) -> dict:
    """Mechanical cleanup only: unknown statuses/events remain rejectable."""
    form = form if isinstance(form, dict) else {}
    out = {"answers": [], "candidate_questions": [], "flag": "none", "stop": form.get("stop") is True,
           "callback": {"requested": False, "time": None}, "roleplay_event": None, "customer_line": None}
    answers = form.get("answers")
    for a in answers if isinstance(answers, list) else []:
        if not isinstance(a, dict) or not _string(a.get("criterion")):
            continue
        out["answers"].append({
            "criterion": _string(a.get("criterion")), "event": _string(a.get("event"), "answer"),
            "status": _string(a.get("status"), "unclear"), "value": _string(a.get("value")),
            "quote": _string(a.get("quote")), "yes_no": a.get("yes_no") if a.get("yes_no") in ("yes", "no") else None,
            "implied": a.get("implied") is True, "condition": _string(a.get("condition")) or None,
            "missing_part": _string(a.get("missing_part")) or None,
        })
    questions = form.get("candidate_questions")
    for q in questions if isinstance(questions, list) else []:
        if isinstance(q, dict) and _string(q.get("text")):
            typ = _string(q.get("type"), "job")
            out["candidate_questions"].append({"text": _string(q.get("text")), "type": typ if typ in QUESTION_TYPES else "job",
                                               "fact_key": _string(q.get("fact_key")) or "unknown"})
    flag = _string(form.get("flag"))
    out["flag"] = flag if flag in FLAGS else "none"
    callback = form.get("callback")
    if isinstance(callback, dict):
        out["callback"] = {"requested": callback.get("requested") is True, "time": _string(callback.get("time")) or None}
    roleplay_event = form.get("roleplay_event", form.get("event"))
    if roleplay_event in ("roleplay_reply", "roleplay_break", "stop"):
        out["roleplay_event"] = roleplay_event
    out["customer_line"] = _string(form.get("customer_line")) or None
    return out


def _tokens(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold(), flags=re.UNICODE)


def _locate_quote(quote: str, message: str) -> tuple[str | None, bool]:
    needle = _tokens(quote)
    haystack = _tokens(message)
    if not needle:
        return None, False
    if any(haystack[i:i + len(needle)] == needle for i in range(len(haystack) - len(needle) + 1)):
        return quote, False
    # A repair must retain real, distinctive content, not just common grammar.
    stopwords = {"i", "im", "i'm", "am", "the", "a", "an", "in", "at", "on", "to", "for", "of", "and", "is", "it", "my", "me", "have", "has", "do", "did", "with", "that", "this", "be", "been", "can"}
    for sentence in re.split(r"(?<=[.!?])\s+|[\n;]+", message):
        words = _tokens(sentence)
        matches = SequenceMatcher(None, needle, words, autojunk=False).get_matching_blocks()
        overlap = sum(m.size for m in matches)
        content = {w for w in needle if w not in stopwords}
        shared_content = content.intersection(words)
        # Allows the spec's "I live in Malad" -> "I stay near ... Malad side".
        distinctive_anchor = any(len(w) >= 4 for w in shared_content)
        if overlap >= max(2, len(needle) * .5) and distinctive_anchor and overlap / max(len(needle), 1) >= .5:
            return sentence.strip(), True
    return None, False


def _history(criterion_id, event, accepted, reason, old, new, quote, message_id, phase="prepare") -> dict:
    return {"criterion_id": criterion_id, "event": event, "accepted": accepted, "reason": reason,
            "old_value": deepcopy(old), "new_value": deepcopy(new), "quote": quote,
            "message_id": message_id, "phase": phase}


def decide(job, state, sheet, form, message, message_id, now, hints=None) -> dict:
    """Validate this message and propose exactly one next action, without I/O."""
    if state.get("state") == "closed":
        raise ValueError("This interview is closed")
    state, sheet = deepcopy(state), deepcopy(sheet)
    form = normalize(form)
    criteria = {c["id"]: c for c in job["criteria"]}
    fixed = job["fixed_lines"]
    original_id = state.get("current_criterion_id")
    original_action = state.get("pending_action")
    original_question = state.get("last_question_asked")
    state.setdefault("counters", {})
    state["counters"].setdefault("abuse_count", 0)
    state["counters"].setdefault("question_only_streak", 0)
    state["counters"].setdefault("unanswered_streak", state["counters"]["question_only_streak"])
    state.setdefault("seen_flags", [])
    state.setdefault("confirmations_asked", [])
    state.setdefault("interrupted_questions", {})
    result = {"state": state, "sheet": sheet, "history": [], "hints": [], "questions": [], "flags": [],
              "saved": [], "facts": [], "unknowns": [], "prefix": [], "question": "", "path": "A", "action": "reask"}
    resumed = state["state"] == "paused_callback"
    if resumed:
        paused_at = state.get("paused_at")
        state["deadline_at"] += max(0, now - (paused_at if paused_at is not None else now))
        state["state"] = state.pop("resume_state", "open")
        state["pending_action"] = state.pop("resume_action", "ask")
        original_action = state["pending_action"]
        original_question = state.pop("resume_question", original_question)
        state["last_question_asked"] = original_question
        state["paused_at"] = None
        result["prefix"].append(fixed["callback_resume"])
    state["last_candidate_msg_at"] = now
    flag = form["flag"]
    route = FLAG_ROUTES.get(flag)
    flag_key = f"{message_id}:{flag}"
    is_new_flag = flag != "none" and flag_key not in state["seen_flags"]
    if is_new_flag:
        state["seen_flags"].append(flag_key)
        if route.get("close_after"):
            counter = f"{flag}_count"
            state["counters"][counter] = state["counters"].get(counter, 0) + 1
    flag_action = route["action"] if route else None
    if route and route.get("close_after") and state["counters"].get(f"{flag}_count", 0) >= route["close_after"]:
        flag_action = "close"
    if is_new_flag:
        result["flags"].append({"type": flag, "quote": message, "message_id": message_id, "action": flag_action})
    if route and flag_action != "close":
        result["prefix"].append(fixed[route["line"]])

    closing_flag = flag_action == "close"
    attempted_current = False
    accepted_elsewhere = []
    changed_criteria = set()
    if not closing_flag and state["mode"] != "roleplay":
        ordered = sorted(form["answers"], key=lambda a: (0 if a["event"] == "correction" else 1 if a["criterion"] == original_id else 2))
        for answer in ordered:
            cid, event = answer["criterion"], answer["event"]
            old = deepcopy(sheet.get(cid))

            def reject(reason):
                result["history"].append(_history(cid, event, False, reason, old, answer, answer["quote"], message_id))

            if cid not in criteria:
                reject("unknown criterion")
                continue
            quote, repaired = _locate_quote(answer["quote"], message)
            if quote is None:
                reject("no supporting text")
                continue
            answer["quote"] = quote
            if event not in EVENTS:
                reject("invalid event")
                continue
            if answer["status"] not in MODEL_STATUSES:
                reject("illegal model status")
                continue
            prior_status = old["status"]
            if event == "answer" and cid != original_id:
                event = "volunteered" if prior_status == "not_asked" else "correction"
            if event == "volunteered" and prior_status != "not_asked":
                event = "correction"
            if event == "correction":
                if prior_status in ANSWERED or old.get("corrected"):
                    pass
                elif prior_status == "not_asked":
                    event = "volunteered"
                elif cid == original_id:
                    event = "answer"
                else:
                    reject("correction targets an unanswered open criterion")
                    continue
            if event == "late_answer" and prior_status not in {"unresolved", "unclear_final", "declined"}:
                reject("late answer requires a previously unresolved criterion")
                continue
            if cid in changed_criteria or old.get("message_id") == message_id:
                reject("duplicate")
                continue
            criterion = criteria[cid]
            status = answer["status"]
            if status == "complete" and answer["implied"]:
                status = "partial"
            if status == "conditional" and not answer["condition"]:
                status = "unclear"
            if criterion["is_yes_no"] and status == "complete" and answer["yes_no"] is None:
                status = "unclear"
            if status == "complete" and answer["missing_part"] in criterion.get("missing_parts", {}):
                status = "partial"
            answer["status"] = status
            answer["event"] = event
            if event == "volunteered" and status != "complete":
                result["hints"].append({"criterion_id": cid, "quote": quote, "message_id": message_id, "resolved": False})
                result["history"].append(_history(cid, event, True, "held as hint" + ("; quote repaired" if repaired else ""), old, old, quote, message_id))
                changed_criteria.add(cid)
                accepted_elsewhere.append(event)
                continue
            confirming = cid == original_id and str(original_action).startswith("confirm")
            confirmed = bool(confirming and status == "complete" and not answer["implied"])
            if criterion["is_knockout"] and status == "complete" and answer["yes_no"] == "no" and not confirmed:
                status = "needs_confirmation"
            row = {
                "criterion_id": cid, "status": status, "value": answer["value"], "quote": quote,
                "yes_no": answer["yes_no"], "condition": answer["condition"],
                "obtained_via": "confirmed" if confirmed else event if event != "answer" else "asked",
                "implied": answer["implied"], "followups_used": old.get("followups_used", 0),
                "corrected": old.get("corrected", False) or event == "correction",
                "confirmed": confirmed, "missing_part": answer["missing_part"], "message_id": message_id,
            }
            sheet[cid] = row
            state["interrupted_questions"].pop(cid, None)
            result["history"].append(_history(cid, event, True, "quote repaired" if repaired else None, old, row, quote, message_id))
            result["saved"].append(deepcopy(row))
            changed_criteria.add(cid)
            if cid == original_id:
                attempted_current = True
                state["counters"]["unanswered_streak"] = 0
            else:
                accepted_elsewhere.append(event)
            for hint in hints or []:
                if hint.get("criterion_id") == cid and not hint.get("resolved"):
                    resolved_hint = deepcopy(hint)
                    resolved_hint["resolved"] = True
                    result["hints"].append(resolved_hint)

    clarify = False
    if not closing_flag:
        fact_map = {fact["key"]: fact for fact in job["facts"]}
        used_facts, used_questions = set(), set()
        for question in form["candidate_questions"]:
            question_key = (question["text"], question["type"])
            if question_key in used_questions:
                continue
            used_questions.add(question_key)
            if question["type"] in {"job", "process"}:
                fact = fact_map.get(question["fact_key"])
                if fact:
                    if fact["key"] not in used_facts:
                        result["facts"].append(deepcopy(fact))
                        used_facts.add(fact["key"])
                else:
                    question["fact_key"] = "unknown"
                    result["unknowns"].append(deepcopy(question))
            elif question["type"] == "clarify":
                clarify = True
            else:
                if fixed["outcome_neutral"] not in result["prefix"]:
                    result["prefix"].append(fixed["outcome_neutral"])
            result["questions"].append(deepcopy(question))

    # Only accepted evidence is persisted before delivery. All progression
    # mutations below have an explicit delivery history phase.
    result["pre_delivery_sheet"] = deepcopy(sheet)

    def update_row(cid, **changes):
        old = deepcopy(sheet[cid])
        sheet[cid].update(changes)
        if old != sheet[cid]:
            result["history"].append(_history(cid, "progression", True, None, old, sheet[cid], sheet[cid].get("quote"), message_id, "delivery"))

    def finish(action, question, cid=None, force_a=False):
        previous_id = state.get("current_criterion_id")
        # A side question or a brief off-topic turn cannot silently turn a
        # pending confirmation back into an ordinary question. The next actual
        # answer must still be attached to the confirmation the user received.
        if action == "reask" and cid == original_id and str(original_action).startswith("confirm"):
            action = original_action
            if original_question:
                question = original_question
            elif original_action == "confirm_implied":
                question = criteria[cid]["confirm_implied_text"]
            elif original_action == "confirm_volunteered":
                question = criteria[cid]["confirm_volunteered_text"]
            else:
                question = criteria[cid]["confirm_text"]
        elif action == "reask" and resumed and cid == original_id and original_question:
            action = original_action or action
            question = original_question
        elif (action == "reask" and cid == original_id and original_action == "followup"
              and original_question and not clarify):
            action, question = "followup", original_question
        row = sheet.get(cid)
        # Old sessions can still contain an implied confirmation. Resume them
        # with a clarification, never by asserting an unknown start date.
        if str(action).startswith("confirm") and row and (
                row.get("implied") or row.get("missing_part") or not _string(row.get("value"))):
            question = criteria[cid].get("missing_parts", {}).get(row.get("missing_part")) or criteria[cid]["simple_question_text"]
            action = "followup"
        rendered = _render_question(question, row)
        if rendered is None:
            if cid is None:
                raise ValueError("Fixed response has an unsupported or unfilled template field")
            question = criteria[cid].get("missing_parts", {}).get((row or {}).get("missing_part")) or criteria[cid]["simple_question_text"]
            rendered = _render_question(question, None)
            if rendered is None:
                raise ValueError("Clarification question must be a complete fixed response")
            action = "followup" if row and row["status"] in {"partial", "unclear"} else "reask"
        question = rendered
        state["current_criterion_id"] = cid
        state["pending_action"] = action
        state["last_question_asked"] = question
        if cid != previous_id:
            state["counters"]["question_only_streak"] = 0
            state["counters"]["unanswered_streak"] = 0
        result["action"] = action
        result["question"] = question
        force_a = force_a or route is not None
        result["path"] = "B" if (result["saved"] or result["facts"] or result["unknowns"]) and not force_a else "A"
        if force_a:
            # Fixed paths still answer grounded questions without another model.
            result["prefix"].extend(f["text"] for f in result["facts"])
            if result["unknowns"]:
                result["prefix"].append(fixed["unknown_deferred"])
        return result

    def close(reason, allow_speech=False):
        for c in _ordered(job):
            cid, row = c["id"], sheet[c["id"]]
            if row["status"] == "needs_confirmation":
                update_row(cid, status="unclear_final", confirmed=False)
            elif row["status"] in {"not_asked", "asked", "off_target"}:
                update_row(cid, status="skipped_time" if reason == "timeout" and c["must_have"] else "unasked")
        state["state"] = "closed"
        state["mode"] = "closed"
        state["close_reason"] = reason
        line = (route.get("close_line", route["line"]) if route and reason == flag
                else f"close_{reason}")
        return finish("close", fixed.get(line, fixed["close_normal"]), force_a=not allow_speech)

    if closing_flag:
        return close(flag)
    if form["stop"] or form["roleplay_event"] == "stop":
        return close("stop")
    if form["callback"]["requested"]:
        state["resume_state"] = state["state"]
        state["resume_action"] = original_action
        state["resume_question"] = original_question
        state["state"] = "paused_callback"
        state["callback_time"] = form["callback"]["time"]
        state["paused_at"] = now
        return finish("pause_callback", fixed["callback"], original_id, force_a=True)
    if now >= state["deadline_at"]:
        return close("timeout")

    def ask_qa():
        state["state"] = "open"
        state["mode"] = "qa"
        return finish("qa", fixed["candidate_qa_prompt"])

    def followup(c):
        cid, row = c["id"], sheet[c["id"]]
        if row["followups_used"] >= c["followup_allowance"]:
            update_row(cid, status="unclear_final")
            return None
        update_row(cid, followups_used=row["followups_used"] + 1)
        # An implied answer is partial evidence, not a value to confirm.
        question = c.get("missing_parts", {}).get(row.get("missing_part"))
        if question:
            return finish("followup", question, cid)
        result["prefix"].append(fixed["unclear_prefix"])
        return finish("followup", c["simple_question_text"], cid)

    def confirm_criterion(c):
        cid, row = c["id"], sheet[c["id"]]
        needs = row["status"] == "needs_confirmation"
        volunteered = (c["is_knockout"] and row["status"] == "complete"
                       and row["obtained_via"] == "volunteered" and not row["confirmed"])
        token = f"{cid}:{row.get('message_id')}"
        if not (needs or volunteered) or token in state["confirmations_asked"]:
            return None
        state["confirmations_asked"].append(token)
        if original_id and cid != original_id and sheet[original_id]["status"] not in RESOLVED:
            if not attempted_current and original_question:
                state["interrupted_questions"][original_id] = {
                    "action": original_action or "ask", "question": original_question,
                }
            else:
                state["interrupted_questions"].pop(original_id, None)
        volunteered = row["obtained_via"] == "volunteered"
        return finish("confirm_volunteered" if volunteered else "confirm",
                      c["confirm_volunteered_text"] if volunteered else c["confirm_text"], cid)

    def ask_criterion(c):
        cid = c["id"]
        row = sheet[cid]
        suspended = state["interrupted_questions"].pop(cid, None)
        if sheet[cid]["status"] == "not_asked":
            update_row(cid, status="asked")
        state["state"] = "open"
        state["mode"] = "screening" if c["must_have"] else "crm"
        if suspended:
            return finish(suspended["action"], suspended["question"], cid)
        if row["status"] in {"partial", "unclear"}:
            return followup(c)
        return finish("ask", c["question_text"], cid)

    def after_roleplay():
        state["state"] = "open"
        if state["deadline_at"] - now > 180:
            for c in _ordered(job):
                if not c["must_have"]:
                    selected = confirm_criterion(c)
                    if selected is not None:
                        return selected
                    if sheet[c["id"]]["status"] not in RESOLVED:
                        selected = ask_criterion(c)
                        if selected is not None:
                            return selected
        return ask_qa()

    def advance():
        for c in _ordered(job):
            if c["must_have"]:
                selected = confirm_criterion(c)
                if selected is not None:
                    return selected
                if sheet[c["id"]]["status"] not in RESOLVED:
                    selected = ask_criterion(c)
                    if selected is not None:
                        return selected
        # This ends the conversation politely; it never generates a verdict.
        if any(c["is_knockout"] and sheet[c["id"]]["status"] == "complete" and sheet[c["id"]]["yes_no"] == "no" and sheet[c["id"]]["confirmed"] for c in job["criteria"]):
            return close("normal", allow_speech=True)
        if not state["roleplay_done"]:
            state["state"] = "roleplay"
            state["mode"] = "roleplay"
            state["roleplay_last_line"] = job["roleplay"]["initial_line"]
            result["prefix"].append(fixed["roleplay_intro"])
            return finish("roleplay_start", state["roleplay_last_line"], force_a=True)
        return after_roleplay()

    if state["mode"] == "roleplay":
        if resumed or route:
            return finish("roleplay_break", state["roleplay_last_line"], force_a=True)
        if form["roleplay_event"] != "roleplay_reply":
            state["roleplay_breaks"] += 1
            if state["roleplay_breaks"] >= 2:
                state["roleplay_done"] = True
                state["roleplay_incomplete"] = True
                result["prefix"].append(fixed["roleplay_incomplete"])
                return after_roleplay()
            result["prefix"].append(fixed["roleplay_break"])
            return finish("roleplay_break", state["roleplay_last_line"], force_a=True)
        state["roleplay_turns"] += 1
        if state["roleplay_turns"] >= 3:
            state["roleplay_done"] = True
            state["roleplay_incomplete"] = False
            result["prefix"].append(fixed["roleplay_complete"])
            return after_roleplay()
        state["roleplay_last_line"] = form["customer_line"] or job["roleplay"]["safe_line"]
        return finish("roleplay_customer", state["roleplay_last_line"], force_a=True)

    # A new volunteered answer waits for its turn in criterion order. A
    # correction to a previously answered criterion may still require an
    # immediate neutral confirmation, preserving the interrupted question.
    for c in _ordered(job):
        cid, row = c["id"], sheet[c["id"]]
        if cid != original_id and row["obtained_via"] == "volunteered":
            continue
        selected = confirm_criterion(c)
        if selected is not None:
            return selected

    if state["mode"] == "qa":
        if resumed and not result["questions"] and not result["saved"]:
            return finish("qa", original_question or fixed["candidate_qa_prompt"], force_a=True)
        return close("normal", allow_speech=True)

    current = criteria.get(original_id)
    if not current:
        return advance()
    row = sheet[original_id]
    if resumed and row["status"] in RESOLVED and not (
        current["is_knockout"] and row["obtained_via"] == "volunteered" and not row["confirmed"]
    ):
        return advance()
    if attempted_current:
        state["counters"]["question_only_streak"] = 0
        state["counters"]["unanswered_streak"] = 0
        if row["status"] in RESOLVED:
            return advance()
        if clarify or route:
            return finish("reask", current["simple_question_text"] if clarify else current["question_text"], original_id, force_a=True)
        if str(original_action).startswith("confirm") and current["is_knockout"]:
            # An ambiguous confirmation must not be interpreted as a refusal.
            update_row(original_id, status="unclear_final", confirmed=False)
            return advance()
        return followup(current) or advance()

    if route or resumed:
        state["counters"]["question_only_streak"] = 0
        return finish("reask", current["question_text"], original_id, force_a=True)
    # Repeating an unanswered question never spends its clarification budget.
    # Count all consecutive no-answer turns separately, including mixed side
    # questions and off-target answers, so alternating them cannot loop forever.
    state["counters"]["unanswered_streak"] += 1
    if state["counters"]["unanswered_streak"] >= UNANSWERED_TURN_LIMIT:
        update_row(original_id, status="unresolved", confirmed=False)
        return advance()
    if accepted_elsewhere and any(event not in {"correction", "late_answer"} for event in accepted_elsewhere):
        if row["status"] in {"not_asked", "asked"}:
            update_row(original_id, status="off_target")
        state["counters"]["question_only_streak"] = 0
        result["prefix"].append(fixed["reask_prefix"])
        return finish("reask", current["simple_question_text"] if clarify else current["question_text"], original_id, force_a=True)
    if result["questions"] or accepted_elsewhere:
        state["counters"]["question_only_streak"] += 1
        return finish("reask", current["simple_question_text"] if clarify else current["question_text"], original_id, force_a=clarify)
    state["counters"]["question_only_streak"] = 0
    result["prefix"].append(fixed["reask_prefix"])
    return finish("reask", current["question_text"], original_id, force_a=True)


def _fact_texts(facts) -> list[str]:
    return [f if isinstance(f, str) else str(f.get("text", "")) for f in facts]


def check_reply(output, facts, unknowns) -> list[str]:
    """Deterministically reject unsafe speech; facts must be copied verbatim.

    This is deliberately stricter than semantic paraphrase checking: exact
    approved fact sentences plus a recruiter deferral cannot invent benefits.
    """
    if not isinstance(output, dict) or not isinstance(output.get("acknowledgement"), str) or not isinstance(output.get("answer_text"), str):
        return ["invalid reply structure"]
    ack, answer = output["acknowledgement"], output["answer_text"]
    combined = f"{ack} {answer}"
    failures = []
    if "?" in ack or "？" in ack:
        failures.append("question in acknowledgement")
    if "?" in answer or "？" in answer:
        failures.append("question in answer text")
    if re.search(r"\b(selected|rejected|shortlist(?:ed|ing)?|hired|pass(?:ed|ing)?|fail(?:ed|ing)?|great|perfect|impressive|excellent|well\s+done|good\s+answer|qualified|unqualified|suitable|unsuitable|ideal|congratulations|strong\s+candidate|right\s+fit|what\s+we\s+need)\b", combined, re.I):
        failures.append("outcome or praise wording")
    fact_texts = _fact_texts(facts)
    allowed_numbers = set(re.findall(r"\d+(?:[.,]\d+)*", " ".join(fact_texts)))
    if set(re.findall(r"\d+(?:[.,]\d+)*", answer)) - allowed_numbers:
        failures.append("number not present in approved facts")
    if answer.strip() and not facts and not unknowns:
        failures.append("answer text without a candidate question")
    if unknowns and "recruiter" not in answer.casefold():
        failures.append("unknown question not deferred")
    if len(combined.split()) > 60:
        failures.append("reply exceeds 60 words")
    remaining = answer.strip()
    for fact in sorted(fact_texts, key=len, reverse=True):
        remaining = remaining.replace(fact, "")
    remaining = remaining.strip()
    if remaining:
        defer_pattern = r"(?:the\s+)?recruiter\s+(?:will|can)\s+(?:confirm|clarify|follow up on)\s+[^.!?]*[.!]?"
        if not unknowns or not re.fullmatch(defer_pattern, remaining, flags=re.I):
            failures.append("answer text is not grounded in approved facts")
    for fact in fact_texts:
        if fact and fact not in answer:
            failures.append("approved fact omitted")
            break
    return failures


def fallback_reply(facts, unknowns) -> dict:
    parts = _fact_texts(facts)
    if unknowns:
        parts.append("The recruiter will confirm the details you asked about.")
    return {"acknowledgement": "Thanks.", "answer_text": " ".join(parts)}


def assemble(result, speech=None) -> str:
    parts = list(result.get("prefix", []))
    if result.get("path") == "B":
        speech = speech if isinstance(speech, dict) else fallback_reply(result.get("facts", []), result.get("unknowns", []))
        parts.extend([speech.get("acknowledgement", ""), speech.get("answer_text", "")])
    parts.append(result.get("question", ""))
    return "\n\n".join(str(part).strip() for part in parts if isinstance(part, str) and part.strip())
