# Grader reliability fix

The first baseline exposed grader problems as well as agent and harness issues. Some claims judgments listed reference facts that the reply never stated. Other checks returned only a generic error, so their original failure cause cannot be recovered.

The revised claims grader extracts exact quotes from the reply and cites supplied facts. The application rejects invented quotes and nonexistent supporting facts, then computes the verdict from the individual support decisions. Pure recruiter deferrals contain no job claims. The prompt distinguishes “includes night shifts” from unsupported promises such as “you can choose your shift” or “you will always work nights.” This improves the grading contract; it does not replace human calibration.

Each call now keeps its input, output schema, redacted provider response, parsed output, request ID, finish reason, usage, and error category in the assertion's **Grader diagnostics**. Timeouts, connection failures, HTTP errors, refusals, truncated output, invalid JSON, schema errors, and invented evidence have distinct explanations. Recoverable failures get one retry, at most two attempts in total. Authentication errors, refusals, and valid negative judgments are not retried. Tokens from rejected outputs and retries are counted; missing usage leaves total cost unknown.

Unavailable checks display **Not verified**, rather than claiming that the agent was wrong. They still count as non-passing checks in existing scores. Historical assertions, baseline denominators, and run `747a06d2d9b7` are unchanged. Version 2 changes the grader contract hash and requires fresh human calibration; historical calibration cannot establish accuracy for this contract.

## Validation

- Regression tests cover the actual invented-claim output from saved case `JOB-01-08`, exact evidence checks, verdict derivation, retries, provider failure categories, credential redaction, assertion trace retention, and unknown costs.
- The complete automated suite passes (178 tests).
- A targeted live check attempted the ten affected saved replies and six synthetic negative controls. All sixteen requests were blocked with HTTP 401, provider code `expired_secret_key`: the locally configured OpenAI API key has expired. These are unavailable checks, not failed model judgments. No agent calls were made, no historical results were rewritten, and live semantic quality remains unverified. The key must be replaced before repeating this validation.

The JSON report in `grader-v2-validation.json` retains the redacted evidence from that attempt. It is a grader-only diagnostic artifact, not a new agent baseline or human calibration report.
