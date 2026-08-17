import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator


load_dotenv()

PSEUDOGRAM_BASE_URL = os.getenv("PSEUDOGRAM_BASE_URL", "https://pseudogram-api.onrender.com")
PSEUDOGRAM_API_KEY = os.getenv("PSEUDOGRAM_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "linkplease.sqlite3")
VERIFY_WEBHOOK_SIGNATURES = os.getenv("VERIFY_WEBHOOK_SIGNATURES", "true").lower() == "true"
MAX_SEND_ATTEMPTS = int(os.getenv("MAX_SEND_ATTEMPTS", "8"))
RATE_LIMIT_SECONDS = float(os.getenv("RATE_LIMIT_SECONDS", "6.2"))
SENDING_TIMEOUT_SECONDS = float(os.getenv("SENDING_TIMEOUT_SECONDS", "30"))
MAX_STATUS_CHECKS = int(os.getenv("MAX_STATUS_CHECKS", "180"))
JOB_CLAIM_TIMEOUT_SECONDS = float(os.getenv("JOB_CLAIM_TIMEOUT_SECONDS", "30"))

JOB_PENDING = 0
JOB_DONE = 1
JOB_CLAIMED = 2


db_lock = threading.RLock()
worker_started = False
worker_wakeup: asyncio.Event | None = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> Any:
    global worker_started, worker_wakeup
    init_db()
    recover_stale_sending(older_than_seconds=0)
    recover_stale_webhook_jobs(older_than_seconds=0)
    worker_wakeup = asyncio.Event()
    if not worker_started:
        worker_started = True
        asyncio.create_task(worker_loop())
    yield


app = FastAPI(title="LinkPlease Assignment", lifespan=lifespan)


class RuleIn(BaseModel):
    keyword: str = Field(min_length=1)
    dm_message: str = Field(min_length=1)

    @field_validator("keyword", "dm_message")
    @classmethod
    def strip_and_require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must contain non-whitespace text")
        return value


class RuleOut(BaseModel):
    rule_id: str
    keyword: str
    dm_message: str


def now() -> float:
    return time.time()


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextmanager
def db() -> Any:
    with db_lock:
        conn = sqlite3.connect(DATABASE_URL, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rules (
                rule_id TEXT PRIMARY KEY,
                keyword TEXT NOT NULL,
                dm_message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS webhook_jobs (
                job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT,
                event_type TEXT NOT NULL,
                comment_id TEXT,
                payload TEXT NOT NULL,
                processed INTEGER NOT NULL DEFAULT 0,
                claimed_at REAL,
                received_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deleted_comments (
                comment_id TEXT PRIMARY KEY,
                deleted_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deliveries (
                delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                comment_id TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL,
                dm_id TEXT,
                send_attempts INTEGER NOT NULL DEFAULT 0,
                send_generation INTEGER NOT NULL DEFAULT 0,
                status_checks INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                locked_at REAL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(rule_id) REFERENCES rules(rule_id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_delivery
            ON deliveries(rule_id, user_id)
            WHERE status != 'cancelled';

            CREATE TABLE IF NOT EXISTS counters (
                name TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rate_limit_state (
                name TEXT PRIMARY KEY,
                last_sent_at REAL NOT NULL
            );
            """
        )
        ensure_column(conn, "deliveries", "status_checks", "status_checks INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "deliveries", "locked_at", "locked_at REAL")
        ensure_column(conn, "webhook_jobs", "claimed_at", "claimed_at REAL")
        conn.execute(
            "INSERT OR IGNORE INTO counters(name, value) VALUES('duplicates_blocked', 0)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO rate_limit_state(name, last_sent_at) VALUES('dm_send', 0)"
        )


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def increment_counter(name: str, amount: int = 1) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO counters(name, value) VALUES(?, ?)
            ON CONFLICT(name) DO UPDATE SET value = value + excluded.value
            """,
            (name, amount),
        )


def verify_signature(raw_body: bytes, signature: str | None) -> None:
    if not VERIFY_WEBHOOK_SIGNATURES:
        return
    if not PSEUDOGRAM_API_KEY:
        raise HTTPException(status_code=500, detail="webhook signing secret not configured")
    if not signature or not signature.startswith("sha256="):
        raise HTTPException(status_code=401, detail="invalid webhook signature")
    expected = hmac.new(PSEUDOGRAM_API_KEY.encode(), raw_body, hashlib.sha256).hexdigest()
    supplied = signature.removeprefix("sha256=")
    if not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="invalid webhook signature")


@app.post("/rules", response_model=RuleOut, status_code=201)
def create_rule(rule: RuleIn) -> RuleOut:
    rule_id = f"rule_{uuid.uuid4().hex[:12]}"
    with db() as conn:
        conn.execute(
            "INSERT INTO rules(rule_id, keyword, dm_message, created_at) VALUES(?, ?, ?, ?)",
            (rule_id, rule.keyword, rule.dm_message, utc_now_iso()),
        )
    return RuleOut(rule_id=rule_id, keyword=rule.keyword, dm_message=rule.dm_message)


@app.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_pseudogram_signature: str | None = Header(default=None),
) -> dict[str, str]:
    raw_body = await request.body()
    verify_signature(raw_body, x_pseudogram_signature)

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc

    event_type = payload.get("event_type")
    data = payload.get("data") or {}
    if event_type not in {"comment.created", "comment.deleted"}:
        raise HTTPException(status_code=400, detail="unsupported event_type")

    with db() as conn:
        conn.execute(
            """
            INSERT INTO webhook_jobs(event_id, event_type, comment_id, payload, received_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                payload.get("event_id"),
                event_type,
                data.get("comment_id"),
                json.dumps(payload, separators=(",", ":")),
                utc_now_iso(),
            ),
        )

    background_tasks.add_task(nudge_worker)
    return {"status": "accepted"}


@app.get("/stats")
def stats() -> dict[str, int]:
    drain_webhook_jobs_for_stats()
    with db() as conn:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status IN ('queued', 'sending', 'accepted') THEN 1 ELSE 0 END) AS queued
            FROM deliveries
            """
        ).fetchone()
        duplicates = conn.execute(
            "SELECT value FROM counters WHERE name = 'duplicates_blocked'"
        ).fetchone()
    return {
        "sent": int(row["sent"] or 0),
        "failed": int(row["failed"] or 0),
        "queued": int(row["queued"] or 0),
        "duplicates_blocked": int((duplicates or {"value": 0})["value"]),
    }


def drain_webhook_jobs_for_stats(max_batches: int = 20, batch_size: int = 100) -> None:
    recover_stale_webhook_jobs()
    for _ in range(max_batches):
        if process_webhook_jobs(limit=batch_size) == 0:
            return


async def nudge_worker() -> None:
    if worker_wakeup is not None:
        worker_wakeup.set()


async def worker_loop() -> None:
    while True:
        try:
            recovered = recover_stale_sending()
            recovered += recover_stale_webhook_jobs()
            processed = process_webhook_jobs()
            reconciled = await reconcile_accepted_deliveries()
            sent = await send_due_deliveries()
            if recovered == 0 and processed == 0 and reconciled == 0 and sent == 0:
                await wait_for_work()
        except Exception as exc:
            print(f"worker error: {exc}", flush=True)
            await asyncio.sleep(1)


async def wait_for_work() -> None:
    if worker_wakeup is None:
        await asyncio.sleep(0.5)
        return
    try:
        await asyncio.wait_for(worker_wakeup.wait(), timeout=0.5)
    except asyncio.TimeoutError:
        pass
    worker_wakeup.clear()


def recover_stale_sending(older_than_seconds: float | None = None) -> int:
    timeout = SENDING_TIMEOUT_SECONDS if older_than_seconds is None else older_than_seconds
    cutoff = now() - timeout
    with db() as conn:
        return conn.execute(
            """
            UPDATE deliveries
            SET status = 'queued', locked_at = NULL, next_attempt_at = ?,
                updated_at = ?, last_error = 'recovered stale send lock'
            WHERE status = 'sending' AND COALESCE(locked_at, 0) <= ?
            """,
            (now(), utc_now_iso(), cutoff),
        ).rowcount


def recover_stale_webhook_jobs(older_than_seconds: float | None = None) -> int:
    timeout = JOB_CLAIM_TIMEOUT_SECONDS if older_than_seconds is None else older_than_seconds
    cutoff = now() - timeout
    with db() as conn:
        return conn.execute(
            """
            UPDATE webhook_jobs
            SET processed = ?, claimed_at = NULL
            WHERE processed = ? AND COALESCE(claimed_at, 0) <= ?
            """,
            (JOB_PENDING, JOB_CLAIMED, cutoff),
        ).rowcount


def process_webhook_jobs(limit: int = 100) -> int:
    jobs = claim_webhook_jobs(limit)

    for job in jobs:
        try:
            payload = json.loads(job["payload"])
            event_type = payload.get("event_type")
            data = payload.get("data") or {}
            comment_id = data.get("comment_id")

            if event_type == "comment.deleted":
                handle_comment_deleted(comment_id)
            elif event_type == "comment.created":
                handle_comment_created(data)

            mark_webhook_job_done(job["job_id"])
        except Exception as exc:
            release_webhook_job(job["job_id"])
            print(f"webhook job {job['job_id']} failed: {exc}", flush=True)

    return len(jobs)


def claim_webhook_jobs(limit: int) -> list[sqlite3.Row]:
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                """
                SELECT job_id FROM webhook_jobs
                WHERE processed = ?
                ORDER BY job_id
                LIMIT ?
                """,
                (JOB_PENDING, limit),
            ).fetchall()
            job_ids = [row["job_id"] for row in rows]
            if not job_ids:
                conn.execute("COMMIT")
                return []
            placeholders = ",".join("?" for _ in job_ids)
            conn.execute(
                f"""
                UPDATE webhook_jobs
                SET processed = ?, claimed_at = ?
                WHERE job_id IN ({placeholders})
                """,
                (JOB_CLAIMED, now(), *job_ids),
            )
            jobs = conn.execute(
                f"SELECT * FROM webhook_jobs WHERE job_id IN ({placeholders}) ORDER BY job_id",
                job_ids,
            ).fetchall()
            conn.execute("COMMIT")
            return jobs
        except Exception:
            conn.execute("ROLLBACK")
            raise


def mark_webhook_job_done(job_id: int) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE webhook_jobs SET processed = ?, claimed_at = NULL WHERE job_id = ?",
            (JOB_DONE, job_id),
        )


def release_webhook_job(job_id: int) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE webhook_jobs SET processed = ?, claimed_at = NULL WHERE job_id = ?",
            (JOB_PENDING, job_id),
        )


def handle_comment_deleted(comment_id: str | None) -> None:
    if not comment_id:
        return
    with db() as conn:
        conn.execute(
            """
            INSERT INTO deleted_comments(comment_id, deleted_at) VALUES(?, ?)
            ON CONFLICT(comment_id) DO NOTHING
            """,
            (comment_id, utc_now_iso()),
        )
        conn.execute(
            """
            UPDATE deliveries
            SET status = 'cancelled', locked_at = NULL, updated_at = ?,
                last_error = 'comment deleted before send'
            WHERE comment_id = ? AND status = 'queued' AND dm_id IS NULL
            """,
            (utc_now_iso(), comment_id),
        )


def handle_comment_created(data: dict[str, Any]) -> None:
    comment_id = data.get("comment_id")
    text = data.get("text") or ""
    user_id = ((data.get("from") or {}).get("user_id")) or ""
    if not comment_id or not user_id:
        return

    with db() as conn:
        if conn.execute(
            "SELECT 1 FROM deleted_comments WHERE comment_id = ?", (comment_id,)
        ).fetchone():
            return
        rules = conn.execute("SELECT * FROM rules ORDER BY created_at, rule_id").fetchall()

    lower_text = text.lower()
    for rule in rules:
        if rule["keyword"].lower() not in lower_text:
            continue
        try:
            created_at = utc_now_iso()
            with db() as conn:
                conn.execute(
                    """
                    INSERT INTO deliveries(
                        rule_id, user_id, comment_id, message, status, next_attempt_at,
                        created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, 'queued', 0, ?, ?)
                    """,
                    (
                        rule["rule_id"],
                        user_id,
                        comment_id,
                        rule["dm_message"],
                        created_at,
                        created_at,
                    ),
                )
        except sqlite3.IntegrityError:
            increment_counter("duplicates_blocked")


async def send_due_deliveries(limit: int = 1) -> int:
    due = now()
    with db() as conn:
        deliveries = conn.execute(
            """
            SELECT * FROM deliveries
            WHERE status = 'queued' AND next_attempt_at <= ?
            ORDER BY delivery_id
            LIMIT ?
            """,
            (due, limit),
        ).fetchall()

    sent = 0
    for delivery in deliveries:
        await send_delivery(delivery)
        sent += 1
    return sent


async def send_delivery(delivery: sqlite3.Row) -> None:
    if comment_was_deleted(delivery["comment_id"]):
        with db() as conn:
            conn.execute(
                """
                UPDATE deliveries
                SET status = 'cancelled', locked_at = NULL, updated_at = ?,
                    last_error = 'comment deleted before send'
                WHERE delivery_id = ? AND status = 'queued'
                """,
                (utc_now_iso(), delivery["delivery_id"]),
            )
        return

    if not PSEUDOGRAM_API_KEY:
        fail_delivery(delivery["delivery_id"], "missing api key")
        return

    locked_at = now()
    with db() as conn:
        updated = conn.execute(
            """
            UPDATE deliveries
            SET status = 'sending', send_attempts = send_attempts + 1,
                locked_at = ?, updated_at = ?
            WHERE delivery_id = ? AND status = 'queued'
            """,
            (locked_at, utc_now_iso(), delivery["delivery_id"]),
        ).rowcount
    if updated == 0:
        return

    await wait_for_rate_limit()
    idempotency_key = f"delivery-{delivery['delivery_id']}-gen-{delivery['send_generation']}"
    payload = {
        "recipient_user_id": delivery["user_id"],
        "message": delivery["message"],
        "comment_id": delivery["comment_id"],
    }
    headers = {"X-API-Key": PSEUDOGRAM_API_KEY, "Idempotency-Key": idempotency_key}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{PSEUDOGRAM_BASE_URL}/v1/dm/send", json=payload, headers=headers
            )
    except httpx.HTTPError as exc:
        schedule_retry(delivery["delivery_id"], delivery["send_attempts"] + 1, str(exc), 10)
        return

    if response.status_code == 202:
        try:
            body = response.json()
        except ValueError:
            schedule_retry(
                delivery["delivery_id"],
                delivery["send_attempts"] + 1,
                "accepted response was not json",
                10,
            )
            return
        dm_id = body.get("dm_id")
        if not dm_id:
            schedule_retry(
                delivery["delivery_id"],
                delivery["send_attempts"] + 1,
                "accepted response missing dm_id",
                10,
            )
            return
        with db() as conn:
            conn.execute(
                """
                UPDATE deliveries
                SET status = 'accepted', dm_id = ?, status_checks = 0, locked_at = NULL,
                    next_attempt_at = ?, updated_at = ?, last_error = NULL
                WHERE delivery_id = ?
                """,
                (dm_id, now() + 2, utc_now_iso(), delivery["delivery_id"]),
            )
        return

    if response.status_code == 429:
        retry_after = parse_retry_after(response.headers.get("Retry-After"), default=10)
        schedule_retry(
            delivery["delivery_id"],
            delivery["send_attempts"] + 1,
            "rate_limited",
            max(retry_after, RATE_LIMIT_SECONDS),
        )
        return

    if response.status_code >= 500:
        schedule_retry(delivery["delivery_id"], delivery["send_attempts"] + 1, "internal_error", 10)
        return

    fail_delivery(delivery["delivery_id"], f"non-retryable response {response.status_code}: {response.text}")


async def wait_for_rate_limit() -> None:
    while True:
        delay = reserve_send_slot()
        if delay <= 0:
            return
        await asyncio.sleep(delay)


def reserve_send_slot() -> float:
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT last_sent_at FROM rate_limit_state WHERE name = 'dm_send'"
            ).fetchone()
            last_sent_at = float(row["last_sent_at"] if row else 0)
            available_at = last_sent_at + RATE_LIMIT_SECONDS
            current = now()
            if current >= available_at:
                conn.execute(
                    """
                    INSERT INTO rate_limit_state(name, last_sent_at) VALUES('dm_send', ?)
                    ON CONFLICT(name) DO UPDATE SET last_sent_at = excluded.last_sent_at
                    """,
                    (current,),
                )
                conn.execute("COMMIT")
                return 0
            conn.execute("COMMIT")
            return available_at - current
        except Exception:
            conn.execute("ROLLBACK")
            raise


def parse_retry_after(value: str | None, default: float) -> float:
    try:
        return max(float(value), 0)
    except (TypeError, ValueError):
        return default


def schedule_retry(delivery_id: int, attempts: int, error: str, delay_seconds: float) -> None:
    if attempts >= MAX_SEND_ATTEMPTS:
        fail_delivery(delivery_id, error)
        return
    with db() as conn:
        conn.execute(
            """
            UPDATE deliveries
            SET status = 'queued', locked_at = NULL, next_attempt_at = ?, updated_at = ?,
                last_error = ?
            WHERE delivery_id = ?
            """,
            (now() + delay_seconds, utc_now_iso(), error, delivery_id),
        )


def fail_delivery(delivery_id: int, error: str) -> None:
    with db() as conn:
        conn.execute(
            """
            UPDATE deliveries
            SET status = 'failed', locked_at = NULL, updated_at = ?, last_error = ?
            WHERE delivery_id = ?
            """,
            (utc_now_iso(), error[:500], delivery_id),
        )


async def reconcile_accepted_deliveries(limit: int = 50) -> int:
    if not PSEUDOGRAM_API_KEY:
        return 0
    due = now()
    with db() as conn:
        deliveries = conn.execute(
            """
            SELECT * FROM deliveries
            WHERE status = 'accepted' AND next_attempt_at <= ? AND dm_id IS NOT NULL
            ORDER BY delivery_id
            LIMIT ?
            """,
            (due, limit),
        ).fetchall()

    for delivery in deliveries:
        await reconcile_delivery(delivery)
    return len(deliveries)


async def reconcile_delivery(delivery: sqlite3.Row) -> None:
    headers = {"X-API-Key": PSEUDOGRAM_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{PSEUDOGRAM_BASE_URL}/v1/dm/{delivery['dm_id']}", headers=headers
            )
    except httpx.HTTPError as exc:
        with db() as conn:
            conn.execute(
                """
                UPDATE deliveries
                SET next_attempt_at = ?, updated_at = ?, last_error = ?
                WHERE delivery_id = ?
                """,
                (now() + 5, utc_now_iso(), str(exc), delivery["delivery_id"]),
            )
        return

    if response.status_code >= 500:
        with db() as conn:
            conn.execute(
                """
                UPDATE deliveries
                SET next_attempt_at = ?, updated_at = ?, last_error = 'status check failed'
                WHERE delivery_id = ?
                """,
                (now() + 5, utc_now_iso(), delivery["delivery_id"]),
            )
        return

    if response.status_code != 200:
        fail_delivery(delivery["delivery_id"], f"status response {response.status_code}: {response.text}")
        return

    try:
        status = response.json().get("status")
    except ValueError:
        with db() as conn:
            conn.execute(
                """
                UPDATE deliveries
                SET next_attempt_at = ?, updated_at = ?, last_error = 'status response was not json'
                WHERE delivery_id = ?
                """,
                (now() + 5, utc_now_iso(), delivery["delivery_id"]),
            )
        return

    if status == "delivered":
        with db() as conn:
            conn.execute(
                """
                UPDATE deliveries
                SET status = 'delivered', updated_at = ?, last_error = NULL
                WHERE delivery_id = ?
                """,
                (utc_now_iso(), delivery["delivery_id"]),
            )
        return

    if status == "failed":
        next_generation = delivery["send_generation"] + 1
        if delivery["send_attempts"] >= MAX_SEND_ATTEMPTS:
            fail_delivery(delivery["delivery_id"], "delivery failed after retries")
            return
        with db() as conn:
            conn.execute(
                """
                UPDATE deliveries
                SET status = 'queued', dm_id = NULL, send_generation = ?,
                    status_checks = 0, locked_at = NULL,
                    next_attempt_at = ?, updated_at = ?, last_error = 'accepted dm later failed'
                WHERE delivery_id = ?
                """,
                (next_generation, now(), utc_now_iso(), delivery["delivery_id"]),
            )
        return

    checks = delivery["status_checks"] + 1
    if checks >= MAX_STATUS_CHECKS:
        fail_delivery(delivery["delivery_id"], "delivery status stayed queued too long")
        return

    with db() as conn:
        conn.execute(
            """
            UPDATE deliveries
            SET status_checks = ?, next_attempt_at = ?, updated_at = ?
            WHERE delivery_id = ?
            """,
            (checks, now() + 2, utc_now_iso(), delivery["delivery_id"]),
        )


def comment_was_deleted(comment_id: str) -> bool:
    with db() as conn:
        return bool(
            conn.execute("SELECT 1 FROM deleted_comments WHERE comment_id = ?", (comment_id,)).fetchone()
        )
