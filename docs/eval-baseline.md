# First live evaluation baseline

GPT-5.6-Sol ran each of the 78 supplied cases once in run `747a06d2d9b7`. Both the understanding and caller used that model; the separate semantic grader was GPT-4.1-Mini.

The four confirmed behavior failures were in the harness, after the model had correctly understood the candidate: the Hinglish underage flag was suppressed; an off-target response consumed a follow-up; volunteered night-shift willingness interrupted location; and an unknown joining date triggered a broken confirmation. All four now pass when their recorded forms are replayed through the corrected harness.

This does not establish that every model answer was correct. Two further cases need expectation review (“correction” versus “answer,” and “fresher” as an implied notice period). Ten cases failed only through unreliable or uncalibrated semantic grader judgments.

## Baseline accuracy, excluding hiring verdicts

| Measure | Passed / total | Baseline |
| --- | --- | --- |
| Conversation cases | 55 / 71 | 77.5% |
| All conversation assertions | 287 / 316 | 90.8% |
| Deterministic code assertions | 280 / 289 | 96.9% |
| Model form assertions | 138 / 140 | 98.6% |
| Confirmed harness failures replayed after fixes | 4 / 4 | 100% regression pass |

These accuracy figures describe the original live run **before harness fixes**, recalculated without final-verdict checks. The four regression replays are offline checks of saved model outputs, not a new live-model accuracy measurement. Semantic-grader results remain provisional; a single pass does not measure repeatability.

Seven verdict-only cases and 19 historical assertions are excluded from conversation accuracy. The mixed `CARRY-04-01` case retains its two conversation checks; only its verdict check and missing-Judge error are excluded. Genuine runtime errors remain failures. New runs skip verdict-only cases and do not invoke the hiring Judge for conversation replays.

Raw historical results and their original metrics remain available. The hosted dashboard imports this baseline idempotently from the compressed archive in `evals/baselines/`; it never makes model calls during startup.
