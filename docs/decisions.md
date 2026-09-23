# Release decisions

The user's latest instruction takes precedence over the original spec: there is no Judge, verdict, score, grader, or eval product. Confirmation holds remain because they support accurate conversational records and neutral question progression.

- A single explicitly fictional customer-support config demonstrates the conversation. No employer facts were supplied, so the app does not present invented facts as a real vacancy.
- The UI is a conversation workbench with an answer sheet and live runtime inspector. It is not a recruiter management dashboard.
- "Delivered" means the browser has rendered the response and sent a delivery acknowledgement. Network transmission alone cannot prove that a person saw it. Unacknowledged replies remain recoverable in the durable outbox.
- Candidate evidence is saved before delivery; next-question state and follow-up consumption commit after delivery acknowledgement.
- Quote repair is conservative and records the repair. It cannot establish that the interpretation is correct.
- Path B facts must use the provided text, which is stricter than merely checking numeric claims. This prevents unsupported nonnumeric additions.
- Safety flags are not diagnoses. Underage flags require explicit age wording; distress and other model flags remain best effort. The bot does not rank or select people.
- Inactive open interviews close after 30 minutes. Callback pauses remain resumable and do not schedule a call.
- Session scope uses a signed browser-owner cookie behind a studio access code. This is a private working release, not a multi-tenant hiring platform.
- The database records schema versions 1 (initial tables) and 2 (the per-attempt token that prevents an expired request from overwriting its retry). Startup applies the additive version 2 migration to an existing version 1 database without deleting records. Schema creation and this migration are idempotent. Later schema changes must introduce explicit migrations instead of deleting or rebuilding production tables.
- Six provider calls is the enforced upper limit per turn. The normal path uses two calls; code tools are local deterministic operations rather than extra model round trips.
