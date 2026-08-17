# Known Failure Modes

- If the deployment uses an ephemeral filesystem and the process restarts, the SQLite database can disappear. A persistent disk or managed database is required to avoid losing queued deliveries, duplicate history, rate-limit state, and stats.
- The SQLite-backed rate limiter only coordinates processes that share the same database file. Multiple deployments or containers with separate database files can exceed PseudoGram's 10-per-minute limit.
- `GET /stats` drains up to 2,000 stored webhook jobs before reporting. If a much larger burst is still backlogged, stats can briefly undercount the remaining unprocessed jobs until the worker or another stats call drains them.
- If PseudoGram accepts a send, returns a network error to this app, and then ignores the same idempotency key on retry, a duplicate DM could be sent. The code uses idempotency keys to prevent that, but it depends on the mock API honoring them consistently.
