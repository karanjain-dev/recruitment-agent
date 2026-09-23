# Hosted evaluation dashboard

The Railway trial allows three services. The existing web service therefore serves the interview studio at `/` and the evaluation dashboard at `/evals/`, with `EVAL_MOUNTED=1`. Each app has its own authentication and database. The shared `/health` endpoint checks both databases.

For a dedicated web service later, use `/railway-evals.toml`. It starts `python run_evals.py`, binds to Railway's `PORT`, uses one worker, and checks `/api/health`.

Use a **new PostgreSQL service with a persistent volume**, separate from the interview database. Evaluation runs, traces, imported datasets and calibration labels/reports are stored there. Each evaluation gets its own temporary conversation database; its evidence is retained in the results database. Deployments do not replace saved results.

Required variables:

| Variable | Value |
| --- | --- |
| `EVAL_HOSTED` | `1` |
| `EVAL_DATABASE_URL` | Reference to the new evaluation Postgres `DATABASE_URL` |
| `EVAL_ACCESS_CODE` | Workspace access code, at least 12 characters |
| `EVAL_COOKIE_SECRET` | Stable signing secret, at least 32 characters |
| `OPENAI_API_KEY` | Server-side model credential |
| `EVAL_SEED_BASELINE` | `1` to import the recorded baseline once |
| `RAILWAY_PUBLIC_DOMAIN` / `EVAL_ALLOWED_HOSTS` | Railway domain / explicit allowed hostnames |

`DATABASE_URL` remains the interview database; `EVAL_DATABASE_URL` must refer to the new evaluation database. The dashboard refuses hosted startup without the database and access secrets. Results, imports, exports and model-run endpoints require login. Cookies are signed, HttpOnly, Secure and SameSite=Strict; write requests must come from the same origin, and failed login attempts are rate limited.

The two databases are separate Railway resources with separate persistent volumes. Both applications share the existing web process. Use the existing studio access code for the eval dashboard; the eval variables reference the studio access secrets.

The archived first run and denominator policy are explained in [eval-baseline.md](eval-baseline.md).
