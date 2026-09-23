# Verification record — 23 September 2026

- Automated suite: **131 tests passed**, including the existing agent tests and new hook, loader, runner, grader, metric, and local API tests.
- Supplied cases, taxonomy, and job configuration are byte-for-byte unchanged.
- Final offline integration run: `3616ad12aa8b` — **78 cases, 308 repetitions, 396 turn traces**. Created through the browser UI and completed through the background runner.
- The diagnostic stub does not read expected answers or case IDs. Its scores measure wiring only, not live model quality.
- All agent errors in that run were explicit `missing_verdict_hook` errors. The existing conversation agent does not implement a hiring Judge. No verdict was fabricated.
- Temporary interview databases were cleaned up; complete traces remain in the separate evaluation results store.
- Browser checks covered run creation, progress, dataset browsing, language filters, failure-to-trace navigation, configuration snapshots, calibration status, comparisons, and case evidence.
- Comparison with earlier development diagnostic `0395d297a280` correctly reports the changed replay outcome and warns that evaluation logic changed. This is an eval-system comparison, not a model comparison.
- At initial build verification, live model validation had **not** been performed. See the subsequent live run below. The attempted full live run was blocked before execution by automatic approval review, pending explicit approval to send supplied cases to OpenAI and incur API usage charges.
- Grader calibration remains uncalibrated. Draft examples require human labels and an agreement run before trust is established.

Run the local workbench with `sh start-evals.sh`; open `http://127.0.0.1:8010`.

## Authorized live single-pass run

After explicit user authorization, run `747a06d2d9b7` completed all 78 cases once
with `gpt-5.6-sol` for both agent stages and the fixed `gpt-4.1-mini` grader.
The prior multi-repeat run was cancelled at the user’s direction.

- Case pass rate: 54/78 (69.2%).
- All-assertion accuracy: 287/335 (85.7%).
- Deterministic non-verdict checks: 280/289 (96.9%).
- Among 24 nonpassing cases: 8 missing-verdict errors, 10 cases failing only
  uncalibrated AI-grader checks, and 6 cases with deterministic behavioral mismatches.
- Several AI-grader judgments contradict their own explanations or falsely
  describe approved wording as unsupported; no stored outcomes were overwritten.
- This is one pass, so it cannot establish flakiness or repeatability.

See `reports/gpt-5.6-sol-single-pass-summary.json` for the breakdown.
