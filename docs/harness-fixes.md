# Harness corrections — 23 September 2026

These changes implement the user's agreed conversation rules. They do not change the supplied evaluation cases or the stored GPT-5.6-Sol run `747a06d2d9b7`.

| Area | Current behavior |
| --- | --- |
| Automatic flags | All six supported flags use one `FLAG_ROUTES` mapping, also used by the model schema. There is no English phrase corroboration. Underage, distress and wrong person close; abuse warns once and closes on a second distinct message; manipulation and identity questions deliver their fixed responses and continue. Flag responses do not invoke the speaking model. |
| Multiple signals | The form has one flag. Understand is instructed to prefer distress, underage, wrong person, abuse, manipulation, then identity. The harness prioritizes closing flags, then stop, callback and time limit before normal progression. |
| Follow-ups | Only requesting clarification of an accepted incomplete current answer increments `followups_used`. A repeat keeps the count unchanged and preserves an already delivered missing-part question. Consumption commits on delivery acknowledgement. |
| No-answer limit | Empty replies, answers to other criteria and side questions count toward a separate `unanswered_streak`. After three consecutive such turns on the same question, mark it unresolved and advance. An accepted current answer or a change of current criterion resets the streak. Automatic tag handling and callback resumption do not consume the limit. |
| Volunteered answers | Save them immediately, finish the current question, and confirm knockout answers when their criterion is reached. A correction to an already answered criterion can still interrupt for confirmation, preserving the original question. |
| Implied answers | Ask for missing information, especially the joining timeframe. Do not confirm a date that the candidate has not supplied. |
| Templates | Only supported `{value}` substitution with a concrete saved answer is allowed. Missing or unsupported fields fall back to a complete clarification question. Invalid fixed closing wording is rejected rather than spoken with an unresolved template. |

## Verification

The four recorded understanding forms from the earlier live run are stored as regression fixtures in `tests/fixtures/harness_regressions.json`. Each is replayed through the shared runtime with an isolated database and mocked provider transport, then checked against its unchanged original dataset expectations:

- `FLAG-01-02`: Hinglish age statement closes with the fixed underage response.
- `READ-04-01`: “30 days” repeats the night-shift question without spending a follow-up.
- `READ-04-02`: volunteered night-shift willingness is saved while location is re-asked.
- `READ-05-02`: leaving a job last month triggers a joining-timeframe follow-up without `{value}` appearing in speech.

Additional tests cover all supported tags, continuation with saved answers, roleplay tags, delayed positive and negative confirmations, correction interruptions, repeat limits, delivery acknowledgement, legacy states, and valid/missing template values.

Run verification with `.venv/bin/python -m pytest -q`. These tests make no provider requests and are not a new model-accuracy measurement. Grader calibration, hiring-verdict functionality and ambiguous model-level expectations are outside this harness change.
