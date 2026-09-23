# Hiring Agent Evals: Build Brief for Codex

## 0. What you are building

An eval product for the screening interview agent. It loads a fixed dataset of test cases, runs the real agent on each case, grades the result, and shows accuracy, failures and traces in a local web UI. Its main use: **swap the agent's model (or prompt) and see exactly what got better and what got worse.**

You are given four files:

| File | What it is |
|---|---|
| `cases.json` | 78 test cases. The ground truth. Do not edit cases to make them pass |
| `taxonomy.json` | 25 failure types with severity, level and how each is detected |
| `job_customer_support_mumbai_v1.json` | The job config every case uses: criteria, job facts, fixed lines, limits |
| `screening-agent-spec.md` | The agent's behaviour spec (conversation layer) |

Naming note: failure types use codes like `JOB-01`, `READ-03`. "F1" in this project always means **F1 score** (precision and recall), never a failure type.

Build it local-only. No auth, no deployment.

---

## 1. Hooks the agent must expose

The eval calls the agent from outside. If any hook is missing, add it to the agent first without changing agent behaviour.

1. **Turn entry point:** `handle_turn(session_id, message) -> TurnResult`. The same function the chat API uses.
2. **Configurable storage:** the agent reads and writes session state through a storage interface that can point at a test database.
3. **Swappable model client:** the model used for call 1 (understand) and call 2 (speak) comes from config, selectable per run.
4. **Structured turn result**, containing at least:
   - `form`: the parsed output of call 1
   - `validator`: for each proposed answer, accepted / repaired / rejected with reason
   - `lookups`: each candidate question, matched fact key or `unknown`
   - `next_action`: a string from the vocabulary in section 4.4
   - `speak_attempts`: every call 2 attempt (raw text, parsed JSON), before any reply-checker change
   - `reply_checks`: each check, pass/fail, per attempt
   - `delivered`: the final message sent
   - `state_before`, `state_after`: full answer sheet and session fields
   - `usage`: latency, input/output tokens per model call
5. **Close + verdict entry point:** `close_and_score(session_id) -> VerdictResult` with the Judge output, the code verdict, the verdict note, and any overrides.
6. **Clock injection:** the session clock must be settable, so tests can start at a given elapsed time and advance it.

---

## 2. Repo layout

```
evals/
  data/
    cases.json
    taxonomy.json
    jobs/customer_support_mumbai_v1.json
  config/
    models.yaml            named model configs for the agent
    grader.yaml            fixed model config for graders
  runner/                  loads cases, seeds state, runs the agent, saves traces
  graders/                 assertion graders + model graders
  metrics/                 computes all metrics from graded results
  store/                   results database (SQLite)
  ui/                      local web app
  cli                      entry commands (section 9)
```

The agent code does not import anything from `evals/`.

---

## 3. The case format

Every case has:

| Field | Meaning |
|---|---|
| `id` | e.g. `READ-03-02` |
| `failure_code`, `failure_name`, `category`, `severity` | From the taxonomy. Severity may be overridden per case |
| `level` | `turn` or `interview` |
| `run_mode` | `turn`, `replay`, or `verdict` (section 4) |
| `kind` | `trigger` (the failure is likely here) or `near_miss` (the correct behaviour is the opposite, catches overcorrection) |
| `language` | `en`, `hinglish`, `hi` |
| `tags` | Free slice tags, e.g. `cause`, `phrasing` |
| `what_we_test` | Plain description, show it in the UI |
| `setup` | The starting state (the snapshot) |
| `input` | One candidate message (turn mode) |
| `script` | List of candidate messages (replay mode) |
| `time_events` | Optional clock jumps in replay: `{after_turn, advance_minutes}` |
| `expected` | The ground truth assertions (section 5) |
| `fail_if` | Plain-language failure conditions, show in the UI |
| `runs` | How many times to run it (5 for severe, 3 otherwise) |

### 3.1 The `setup` block

| Field | Meaning |
|---|---|
| `job` | Job config ID |
| `mode` | `questions`, `roleplay`, `qa`, `closed` |
| `current_criterion` | The criterion being asked |
| `pending_action` | `ask` or `confirm` |
| `elapsed_minutes` | Clock position at the start |
| `question_only_streak` | Counter value at the start |
| `answer_sheet` | All five criteria, each with `status, value, quote, yes_no, condition, obtained_via, followups_used, confirmed, implied, corrected` |
| `recent_messages` | Earlier candidate messages, oldest first |
| `hints` | Earlier hints, if any |

Verdict-mode cases also carry `close_reason` and `roleplay_transcript`.

---

## 4. Running a case

### 4.1 Turn mode (65 cases)

For each run:
1. Validate the case against the schema. Invalid cases fail loudly and are not run.
2. Create a **fresh session in a test SQLite database**. Never the main database.
3. Seed every `setup` field. Then check the seeded state is legal (for example, a `complete` row must have a quote). Abort the run if not.
4. Set the clock to `elapsed_minutes`.
5. Call `handle_turn(session_id, input)`.
6. Save the full `TurnResult` as the trace.

### 4.2 Replay mode (6 cases)

1. Seed the `setup` (a fresh interview at the first question).
2. Send each `script` message in order with `handle_turn`, saving a trace per turn.
3. Apply any `time_events` after the named turn.
4. Stop when the script ends or the session closes. If the session is still open, call `close_and_score`.
5. Grade the **final** state and the list of questions asked. Scripts are fixed: they are sent in order even if the agent asks something unexpected. That mismatch is itself evidence.

### 4.3 Verdict mode (7 cases)

Seed the closed `answer_sheet`, `close_reason` and `roleplay_transcript`, then call `close_and_score`. Grade the verdict only.

### 4.4 `next_action` vocabulary

The agent must report its chosen next step as one of:

`ask:<criterion>` · `reask:<criterion>` · `followup:<criterion>` · `simple_reask:<criterion>` · `confirm:<criterion>` · `roleplay` · `qa` · `close:<reason>`

Where `reask` means the same question again without using a follow-up, `followup` means a follow-up was consumed, `simple_reask` means the simpler approved wording.

### 4.5 Repeats and isolation

Run each case `runs` times, each from a fresh session. Nothing carries over between runs.

### 4.6 Versions pinned on every run

Store with every run: agent model config name and model ID, temperature, prompt versions (hash of prompt text for call 1 and call 2), job config version, dataset version, git commit, grader model ID.

**The grader model must stay fixed while the agent model changes.** Otherwise a comparison measures two things at once.

---

## 5. Grading: exact semantics of `expected`

Grade every run against its case's `expected` block. Each key below is one assertion. A run passes only if every assertion passes. Store each assertion's result with a readable reason.

### 5.1 `form` (compared to call 1's parsed form)

| Key | Passes when |
|---|---|
| `answers: []` | The form contains **zero** answers |
| `answers: [items]` | Each expected item matches at least one actual answer. Extra actual answers are allowed but logged as warnings |
| item `criterion` | Equal |
| item `event_in` | Actual event is in the list |
| item `status_in` | Actual status is in the list |
| item `yes_no` / `yes_no_in` | Equal / in list (`null` allowed in lists) |
| item `implied` | Equal |
| item `has_condition` | `true` means condition is non-empty |
| item `value_contains` | Case-insensitive substring of value |
| item `missing_part` | Equal |
| `no_answer_for: [criteria]` | No actual answer has that criterion |
| `questions: [items]` | Each expected item matches an actual candidate question. `type_in` list, `fact_key` equal, `fact_key_in` list |
| `flag` | Equal |

### 5.2 `state_after` (compared to the answer sheet after the turn)

Keys are `criterion.field`:

| Key form | Passes when |
|---|---|
| `x.field` | Equal |
| `x.status_in` | Status in list |
| `x.value_contains` | Case-insensitive substring |
| `x.quote_in_message` | The saved quote, normalised (lowercase, punctuation stripped, spaces collapsed), is a substring of the candidate's message |

### 5.3 `next_action` / `next_action_in`

Equal / in list.

### 5.4 `reply`

Graded **twice**: on the first speak attempt (the model's raw output, before any checker change) and on the delivered message. Record both. For Path A turns with no speak attempt, grade the delivered message only.

| Key | Passes when |
|---|---|
| `must_mention_any` | At least one phrase appears (case-insensitive) |
| `must_include_all` | Every string appears. Normalise numbers: `20,000` matches `20000` and `₹20,000` |
| `must_not_contain` | No phrase appears |
| `must_not_contain_words` | No listed word appears as a whole word |
| `numbers_from_facts` | Every number in the reply appears in some job fact text |
| `no_question_in_ack` | The acknowledgement contains no `?` |
| `question_marks_exactly` | The delivered message has exactly N `?` |
| `claims_supported_by_facts` | Model grader, section 6.1 |
| `must_not_claim` | Model grader: the reply does not assert any listed claim |
| `grader_no_praise` | Model grader, section 6.2 |

### 5.5 Replay keys

| Key | Passes when |
|---|---|
| `final_sheet.x.{field}` | Same rules as `state_after`, on the final sheet |
| `never_asked: [criteria]` | No `ask/reask/followup/simple_reask` action targeted that criterion at any turn |
| `asked_at_most_once: [criteria]` | At most one `ask` or `reask` for that criterion across the interview (`followup` excluded) |
| `close_reason` | Equal |
| `all_must_haves_terminal` | Every must-have ends in: complete, conditional, declined, unclear_final, unresolved, skipped_time |
| `verdict_not` | Final verdict is not this value |

### 5.6 Verdict keys

`verdict` equal, `verdict_not` not equal, `verdict_note_contains` substring of the note.

### 5.7 `grader` block

`value_supported_by_quote: true`: model grader, section 6.3.

---

## 6. Model graders

Use the fixed grader model from `grader.yaml`. Temperature 0. Every grader returns JSON with `pass` (bool) and `reason`.

### 6.1 Claim support
Input: the reply text and the full list of job fact texts.
Instructions: list every statement the reply makes about the job (pay, timings, location, transport, benefits, policies, choices available to the candidate). For each, decide whether one job fact directly supports it. A statement that extends a fact beyond what it says (for example "rotational shifts" becoming "you can choose your shift") is unsupported. Deferring to the recruiter is not a claim.
Pass when every claim is supported. Return the list of claims with supported yes/no.

### 6.2 No praise or outcome
Input: the acknowledgement.
Pass when it contains no praise, judgement of answer quality, or hint about selection or rejection, in any language.

### 6.3 Value supported by quote
Input: criterion, saved value, saved quote.
Pass when the quote on its own supports the value.

### 6.4 Calibration (required before model-grader results are shown as trustworthy)
Provide a calibration mode: a file of 30 hand-labelled examples per model grader (reply + human label). Run the grader on them and show agreement. The UI must show each model grader's agreement rate and mark its results as "uncalibrated" until agreement is at least 90%.

---

## 7. Metrics

### 7.1 Run, case, failure type

| Metric | Definition |
|---|---|
| Run pass | All assertions pass |
| Case pass rate | Passed runs ÷ runs |
| Case status | **Severe:** pass only if every run passes. **Other:** pass if more than half the runs pass |
| Flaky case | Some runs pass and some fail. Show separately |
| Failure-type score | Cases passed ÷ cases for that code |
| Assertion accuracy | Passed assertions ÷ total assertions |
| Severe failure count | Number of severe cases not passing. The headline number |

### 7.2 Model rate vs product rate
For every `reply` assertion: failure rate on the first speak attempt (model rate) and on the delivered message (product rate). The gap shows how much the reply checker is doing.

### 7.3 Classification metrics with precision, recall and F1
Compute a confusion matrix and precision / recall / F1 for each of these. Use only cases where the expected value is a single definite label.

| Classifier | Positive class | Source |
|---|---|---|
| Flag detection | Each flag type, one-vs-rest, including `none` | `expected.form.flag` vs actual |
| Knockout refusal | `yes_no = no` on night_shifts | Expected answers with `yes_no` |
| Complete detection | `status = complete` | Expected answers whose `status_in` is exactly one value |
| Fact matching | Known fact vs `unknown` | `expected.form.questions` with `fact_key` |

### 7.4 Slices
Every metric above can be grouped by: `category`, `failure_code`, `severity`, `language`, `kind`, `level`, and each key in `tags`.

### 7.5 Cost and speed
Per run: total latency, tokens, cost (from a price table in `models.yaml`). Report p50 / p95 latency and cost per case and per run.

---

## 8. The UI (local web app, six screens)

Plain, readable tables. No charts needed.

### 8.1 Runs
List of eval runs: run ID, date, agent model config, dataset version, severe failure count, case pass rate, cost. Select two to compare.

### 8.2 Run dashboard
- Headline: severe failures, case pass rate, assertion accuracy, cost, p95 latency
- Table by category and by failure code: cases, passed, failed, flaky
- Classification table: precision, recall, F1 per classifier, with the confusion matrix on click
- Model rate vs product rate for reply assertions
- Slice table: pick a slice dimension, see all metrics split by it
- Warning banner if any model grader is uncalibrated

### 8.3 Failures
Every failing case, sorted severe first. Filters: category, code, severity, language, kind, tag. Each row: case ID, title, how many runs failed, the first failing assertion and its reason. Click opens the case detail.

### 8.4 Case detail
- Top: title, what we test, fail_if, setup (answer sheet as a table, recent messages), input or script, expected
- Below: every run of this case. For each run, the full trace step by step: form, validator decisions, lookups, next action, each speak attempt, reply checks, delivered message, state before and after. Assertions listed with pass/fail and reason
- For replay cases: turn-by-turn traces and the question sequence

### 8.5 Compare
Pick run A and run B (for example the same dataset on two models). Show:
- Headline deltas
- Per failure code and per slice deltas
- **Regressions:** cases that passed in A and fail in B, severe first
- **Fixes:** cases that failed in A and pass in B
- Click any case to see A's and B's traces side by side

### 8.6 Dataset
Browse all cases with the same filters, showing input, expected and fail_if. Read-only.

---

## 9. Commands

```
evals run --agent-model <config-name> [--cases <glob or code>] [--runs <n override>]
evals compare <run_id_A> <run_id_B>
evals calibrate --grader <claims|praise|quote>
evals ui
```

Swapping the model is only ever `--agent-model`. The dataset, job config and grader stay fixed.

---

## 10. Rules

- Never edit a case to make it pass. If a case looks wrong, add it to a `disputed.json` list with a reason. Disputed cases stay in results but are shown separately.
- The test database is created per run and deleted afterwards unless `--keep-db` is set.
- Every result row must link to its trace.
- A missing hook or a crash in the agent is a failed run with reason `agent_error`, not a skipped run.

---

## 11. Build order and done-when

| Step | Build | Done when |
|---|---|---|
| 1 | Hooks in the agent (section 1) | A manually created session returns a full `TurnResult` |
| 2 | Case schema validation + loader | All 78 cases load; a deliberately broken case fails with a clear message |
| 3 | Turn-mode runner with a **stub** model client | `READ-01-01` runs with a stub and the seeded state matches `setup` exactly |
| 4 | Real model, repeats, version pinning | `JOB-01-02` runs 5 times with versions stored on each run |
| 5 | Assertion graders (5.1 to 5.6) | Changing an expected value in a copy of a case makes it fail with a readable reason |
| 6 | Model graders + calibration mode | Calibration screen shows agreement on the labelled file |
| 7 | Replay and verdict modes | `CARRY-01-04` and `VERDICT-01-02` run end to end |
| 8 | Metrics (section 7) | Numbers on the dashboard match a hand count on a small run |
| 9 | UI screens 8.1 to 8.4, 8.6 | You can go from the dashboard to any failing run's trace in two clicks |
| 10 | Compare (8.5) | Two runs with different agent models show regressions and fixes correctly |

## 12. Acceptance for the whole product

1. `evals run --agent-model A` then `--agent-model B` on the full dataset completes, and `evals compare` lists every case that changed status.
2. Every severe failure on the dashboard opens to a trace showing which step went wrong.
3. Precision, recall and F1 appear for all four classifiers.
4. Model rate and product rate appear side by side for reply assertions.
5. Every metric can be split by language and by kind.
6. Nothing in `agent/` was changed except adding the hooks.