# Grader calibration

Each JSON file contains 30 **AI-drafted, unlabelled** examples. These are review worksheets, not evidence of accuracy. No model-grader result is trustworthy until a human reviews these examples and the fixed grader reaches at least 90% agreement.

For each example, set `human_label` to a JSON boolean (`true` means the assertion should pass) and `reviewed` to `true`. At the file level set `label_source` to `human_reviewed` and enter the reviewer's name in `reviewed_by`. Adapt examples where needed before reviewing them; evaluation cases themselves remain immutable.

Run `python -m evals calibrate --grader claims` (or `praise`, `quote`, `must_not_claim`). This creates a report with each prediction, reason, agreement, label hash, grader model ID, and prompt hash. Changing labels, the grader model, or its instructions invalidates the report. At least 30 reviewed examples are required. Network failures count as disagreements, never successful grades.

The application never supplies human labels on your behalf.
