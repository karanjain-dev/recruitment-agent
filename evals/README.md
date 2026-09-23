# Screening agent evaluations

A local or hosted workbench for versioned datasets, repeatable agent runs, assertion-level
traces, model/prompt experiments, and saved comparisons. The supplied 78 cases and
25 taxonomy entries are copied byte-for-byte and remain read-only. The default
source repeat policy describes 308 attempts. Final-verdict cases are now excluded
from execution and accuracy; the active conversation suite contains 71 cases.
See [the baseline](../docs/eval-baseline.md) and [hosted deployment](../docs/eval-hosting.md).

## Run locally

From the project directory, use the existing virtual environment:

```sh
.venv/bin/python -m evals ui
```

Open `http://127.0.0.1:8010`. No deployment or authentication is required; the
server binds only to the loopback interface. Configure `OPENAI_API_KEY` in the
project's existing `.env` file or the process environment. Credentials are read
locally and never included in saved configurations, traces, or browser responses.

Create a run from the dashboard: choose a dataset and named model, optionally
select different understanding and caller models, edit either prompt, and choose
a case selection or repeat override. Original prompts remain unchanged. The run
stores the full prompt text, its hashes, job and dataset snapshots, grader
configuration, source hashes, and git revision so later changes do not rewrite
history. The fixed grader configuration does not change when the agent changes.

```sh
.venv/bin/python -m evals run --agent-model gpt-4.1-mini --cases READ-01-01 --runs 1
.venv/bin/python -m evals run --agent-model gpt-4.1-mini --cases JOB-01-02 --runs 5
.venv/bin/python -m evals run --agent-model gpt-4.1 --name "Larger model"
.venv/bin/python -m evals compare RUN_A RUN_B
.venv/bin/python -m evals calibrate --grader claims
```

`--cases` accepts a case ID, failure code, glob (quote it in your shell), or
comma-separated selections. The default uses each case's original repeat count.
`--understand-prompt PATH` and `--speak-prompt PATH` load an experiment's prompt
overrides; `--understand-model`, `--speak-model`, and `--temperature` are also
available. `--store PATH` selects a separate evaluation results database.

For network-free wiring checks:

```sh
.venv/bin/python -m evals run --agent-model offline-stub --runs 1
```

These runs are prominently marked diagnostic. The stub sees normal agent input,
never case expectations. Its scores are not evidence of a live model's quality.
Model-graded assertions fail as unavailable in this mode.

## Storage and isolation

Eval results are stored in `evals/store/results.sqlite3`, separately from the
interview application. Every suite receives a temporary session SQLite database;
every repetition seeds a new interview from the original case snapshot. Replay
time events advance an injected clock. The temporary database is deleted after
execution; `--keep-db` preserves it and records its path. Saved result rows always
contain their traces, even when the temporary database is removed.

Each completed attempt commits before the next starts. Restarting the dashboard
marks unfinished runs interrupted and preserves their evidence. Cancellation
finishes the current attempt and stops before the next one. A failed model call,
crash, or missing hook is an `agent_error` failure, never a skipped success.

The current agent is a conversation agent: it has no hiring Judge implementation.
Verdict-only cases and verdict assertions inside mixed cases are outside accuracy.
New conversation runs never invoke the Judge. Historical missing-Judge failures
are excluded only where caused by final-verdict checks; other errors still fail.
The raw historical evidence and original metrics are retained alongside the
current conversation-only metric view.

Hosted mode uses the dedicated `EVAL_DATABASE_URL` PostgreSQL database for runs,
traces, uploaded datasets, and calibration labels/reports. It requires access-code
login and stable cookie signing. Temporary per-run conversation databases remain
isolated and disposable; their traces are retained in the results store.

The supplied eval job omits roleplay configuration and several operational fixed
lines. The runtime inherits those omitted fields from the current agent config
and records the adaptation warning in each trace. Candidate facts and criterion
definitions come from the supplied job. Unsupported limit changes are disclosed.

## New datasets and evaluation kinds

The Dataset page imports a JSON bundle containing `id`, `name`, `version`,
`cases`, `taxonomy`, and `job`. `cases` may be the original cases wrapper or a
list. Imports are validated before being added; IDs are unique and immutable.
Use a new ID when publishing a new version. The source bundle is never edited.

Validation checks IDs, taxonomy relationships, supported run modes, snapshots,
criterion completeness, quotes on complete answers, script messages, clock
events, repeat counts, and assertion keys. Unknown or malformed cases fail before
any provider calls. The built-in run modes are `turn`, `replay`, and `verdict`.
To add a new evaluation kind, add its mode and payload rules to
`evals/datasets.py:validate_dataset`, its execution branch to
`evals/runner/__init__.py:execute_run`, and its assertion logic to
`evals/graders/assertions.py:grade_case`. For a new assertion on an existing mode,
extend `EXPECTED_KEYS` and `_validate_expected` in the dataset validator, then
add its handler to `grade_case`. Add a focused success/failure test for the new
behavior. Results keep the shared `{path, stage, passed, reason, expected, actual}`
assertion shape and the existing trace contract; storage and case-detail UI then
work unchanged. Add a classifier or slice calculation to
`evals/metrics/aggregate.py` only when the new kind needs a new aggregate.

Disputed cases are read from `evals/data/disputed.json` without modifying the
original cases. The file is a JSON list of records such as
`{"case_id":"READ-01-01","reason":"Explain the disputed ground truth"}`.
Add `"dataset_id":"your-dataset-id"` for an imported dataset; omission means the
bundled dataset. The loader annotates affected cases and their result summaries,
keeps them in all totals, and exposes a separate disputed-case summary. The
dispute annotations are frozen with each run and separately hashed. Review the
underlying source before making any dataset revision.

## Model graders and prices

`evals/config/models.yaml` and `grader.yaml` use JSON syntax (valid YAML) so no
additional YAML dependency is needed. Model pricing is an editable estimate in
USD per million input/output tokens. Unknown model prices are shown as unknown,
not free. Saved usage includes grader calls as well as agent calls.

Calibration files under `evals/data/calibration` contain draft examples for human
review. They are **not hand-labelled gold data**. A model grader remains
uncalibrated until at least 30 examples have explicit reviewed human labels and
a matching calibration run reaches at least 90% agreement. Trust is tied to the
fixed model, grader prompt, and label-file hashes. The dashboard must retain this
warning until the requirement is met.

## Developer interfaces

```python
from evals.datasets import list_datasets, get_dataset, import_dataset
from evals.runner import prepare_run, execute_run
from evals.store import Store

store = Store()
run = prepare_run({"agent_model": "offline-stub", "cases": "READ-01-01", "runs": 1}, store)
completed = await execute_run(run["id"], store=store)
trace = store.get_case_detail(run["id"], "READ-01-01")
```

The agent does not import the `evals` package. `execute_run` accepts injected
runtime, grader, grading function, and metric function for focused tests.
