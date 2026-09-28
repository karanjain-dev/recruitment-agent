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

## Live validation after replacing the expired key

On September 28, the replacement key allowed real GPT-4.1-mini grader calls. The first check produced 15/16 expected decisions: all ten saved replies passed, but a synthetic invented-cab claim was incorrectly accepted based on a night-shift fact. This was a semantic grader mistake, not a request failure.

Version 2.1 now explicitly requires evidence for the entire claim. Related topics, plausibility, and absence of contradiction do not establish support. The prompt also separates reply quotes from reference facts, preserves unsupported details in compound claims, and specifies null evidence for unsupported claims.

The revised grader matched all **20/20** targeted expectations, on the first attempt for every check, with **zero unavailable results**: ten saved replies, eight unsupported-claim controls, and two supported transport controls. The controls include invented cab and meal benefits, shift choice, permanent nights, joining bonus, false denial of night shifts, mixed claims, and Hinglish transport. It accepts transport claims when transport evidence is actually supplied.

This is a small developer-designed regression check, not human calibration or a new measurement of the agent's accuracy. The original baseline is unchanged. The complete automated suite still passes (178 tests). Redacted evidence is retained in `grader-v2-validation-key-check.json` (15/16 before refinement) and `grader-v2-1-validation.json` (20/20 after refinement). The historical expired-key artifact is retained separately.
