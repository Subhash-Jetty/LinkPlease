# Submission Checklist

## Done in this repo

- Required routes: `POST /rules`, `POST /webhook`, `GET /stats`
- Part A: matching rules, durable queue, duplicate blocking by `(rule_id, user_id)`
- Part B: HMAC webhook signature verification, live stats
- Part C: delivery reconciliation, retry after later DM failure, `comment.deleted` handling, burst-safe queueing, conservative rate limiting
- Required `FAILURES.md`
- Tests for contract and important edge cases
- Render blueprint with persistent disk config

## Before submitting

1. Push this folder to a public GitHub repo.
2. Deploy from GitHub using Render or another public host.
3. Set `PSEUDOGRAM_API_KEY` in the deployment environment.
4. Confirm the deployed URL responds at `/stats`.
5. Create at least one rule on the deployed URL.
6. Run a 500-event simulation against the deployed URL.
7. Compare `/stats` with PseudoGram truth.
8. Record the 3-minute Loom.
9. Submit to PseudoGram.

## Useful commands

Create a deployed rule:

```powershell
Invoke-RestMethod -Method Post -Uri "https://YOUR-APP.onrender.com/rules" -ContentType "application/json" -Body '{"keyword":"PRICE","dm_message":"Here is the price list"}'
```

Run simulation:

```powershell
python scripts/pseudogram.py simulate --webhook-url "https://YOUR-APP.onrender.com" --count 500 --duration-seconds 10
```

Fetch truth:

```powershell
python scripts/pseudogram.py truth --run-id "RUN_ID_FROM_SIMULATION"
```

Submit:

```powershell
python scripts/pseudogram.py submit --email "subhash_jetty@gmail.com" --github-repo "https://github.com/YOU/REPO" --working-url "https://YOUR-APP.onrender.com" --loom-url "https://loom.com/share/..." --parts-completed "A+B+C" --start-date "2026-08-17"
```

