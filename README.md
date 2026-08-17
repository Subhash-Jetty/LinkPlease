# LinkPlease Tech Intern Assignment

FastAPI implementation of the required LinkPlease/PseudoGram API contract.

## Routes

- `POST /rules`
- `POST /webhook`
- `GET /stats`

The webhook route persists incoming events quickly and returns immediately. A background worker matches comments to rules, blocks duplicate user/rule sends, retries transient failures, respects the 10-per-minute send limit, recovers stale in-flight sends after crashes, and reconciles accepted DMs until they become delivered or failed.

## Run Locally

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
set PSEUDOGRAM_API_KEY=your_key_here
uvicorn app:app --reload
```

For PowerShell, set the key with:

```powershell
$env:PSEUDOGRAM_API_KEY = "your_key_here"
```

## Deploy

This repo includes a `Procfile` for Render-style deployment:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

Set `PSEUDOGRAM_API_KEY` in the deployment environment. The app stores durable queue state in `linkplease.sqlite3` by default. For production, mount persistent disk storage or set `DATABASE_URL` to a persistent SQLite file path.

`runtime.txt` pins Python 3.12 for hosts that honor it.

There is also a `render.yaml` blueprint that configures the app with a persistent disk at `/var/data`.

## Notes

- Keyword matching is case-insensitive and matches anywhere in the comment text.
- Duplicate suppression uses the stable pair `(rule_id, user_id)`, not username or event id.
- Signature verification is on by default; `VERIFY_WEBHOOK_SIGNATURES=true` requires `PSEUDOGRAM_API_KEY`.
- When `REQUIRE_WEBHOOK_SIGNATURES=false`, invalid signatures are rejected when present, but unsigned simulator webhooks are accepted.
- `COMMENT_DELETE_POLICY=ignore` keeps delivery accounting aligned with PseudoGram's expected unique recipients. Set it to `cancel` to suppress sends for deleted comments.
- `GET /stats` is computed from durable delivery rows plus a durable duplicate counter.
- The send-rate limiter is backed by SQLite, so it survives process restarts when the database file is persistent.
