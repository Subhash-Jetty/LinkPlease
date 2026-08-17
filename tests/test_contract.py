import asyncio
import hashlib
import hmac
import importlib
import json

from fastapi.testclient import TestClient


def load_app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("PSEUDOGRAM_API_KEY", "secret")
    monkeypatch.setenv("VERIFY_WEBHOOK_SIGNATURES", "true")
    monkeypatch.setenv("COMMENT_DELETE_POLICY", "cancel")
    monkeypatch.setenv("RATE_LIMIT_SECONDS", "0")
    monkeypatch.setenv("SENDING_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("MAX_STATUS_CHECKS", "3")

    import app

    importlib.reload(app)
    app.init_db()
    return app


def signed_headers(body: bytes) -> dict[str, str]:
    sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    return {"X-PseudoGram-Signature": f"sha256={sig}"}


def comment_payload(
    *,
    event_id: str = "evt_1",
    comment_id: str = "cmt_1",
    user_id: str = "usr_1",
    text: str = "what is the price?",
    event_type: str = "comment.created",
) -> dict:
    data = {"comment_id": comment_id}
    if event_type == "comment.created":
        data |= {
            "post_id": "post_1",
            "text": text,
            "created_at": "2026-08-10T09:14:21.900Z",
            "from": {"user_id": user_id, "username": "person"},
        }
    return {
        "event_id": event_id,
        "event_type": event_type,
        "sent_at": "2026-08-10T09:14:22.481Z",
        "data": data,
    }


def post_signed_webhook(client: TestClient, payload: dict):
    body = json.dumps(payload).encode()
    return client.post("/webhook", content=body, headers=signed_headers(body))


class FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None, headers: dict | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakePseudoGramClient:
    post_responses: list[FakeResponse] = []
    get_responses: list[FakeResponse] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        return self.post_responses.pop(0)

    async def get(self, *args, **kwargs):
        return self.get_responses.pop(0)


def test_create_rule_contract(tmp_path, monkeypatch):
    app_module = load_app(tmp_path, monkeypatch)
    client = TestClient(app_module.app)

    response = client.post("/rules", json={"keyword": "PRICE", "dm_message": "Here you go"})

    assert response.status_code == 201
    body = response.json()
    assert body["rule_id"].startswith("rule_")
    assert body["keyword"] == "PRICE"
    assert body["dm_message"] == "Here you go"


def test_create_rule_trims_whitespace(tmp_path, monkeypatch):
    app_module = load_app(tmp_path, monkeypatch)
    client = TestClient(app_module.app)

    response = client.post("/rules", json={"keyword": "  PRICE  ", "dm_message": "  Here you go  "})

    assert response.status_code == 201
    assert response.json()["keyword"] == "PRICE"
    assert response.json()["dm_message"] == "Here you go"


def test_create_rule_is_idempotent_by_keyword(tmp_path, monkeypatch):
    app_module = load_app(tmp_path, monkeypatch)
    client = TestClient(app_module.app)

    first = client.post("/rules", json={"keyword": "PRICE", "dm_message": "Here you go"})
    second = client.post("/rules", json={"keyword": "price", "dm_message": "Different"})

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()


def test_webhook_matches_rule_and_blocks_duplicate_user_rule(tmp_path, monkeypatch):
    app_module = load_app(tmp_path, monkeypatch)
    client = TestClient(app_module.app)
    client.post("/rules", json={"keyword": "PRICE", "dm_message": "Here you go"})

    payload = comment_payload()
    response = post_signed_webhook(client, payload)
    assert response.status_code == 200
    app_module.process_webhook_jobs()

    duplicate = comment_payload(event_id="evt_2", comment_id="cmt_2")
    response = post_signed_webhook(client, duplicate)
    assert response.status_code == 200
    app_module.process_webhook_jobs()

    stats = client.get("/stats").json()
    assert stats == {"sent": 0, "failed": 0, "queued": 1, "duplicates_blocked": 1}


def test_rejects_bad_signature(tmp_path, monkeypatch):
    app_module = load_app(tmp_path, monkeypatch)
    client = TestClient(app_module.app)

    response = client.post(
        "/webhook",
        json={"event_type": "comment.created", "data": {"comment_id": "cmt_1"}},
        headers={"X-PseudoGram-Signature": "sha256=bad"},
    )

    assert response.status_code == 401


def test_stats_drains_pending_webhook_jobs(tmp_path, monkeypatch):
    app_module = load_app(tmp_path, monkeypatch)
    client = TestClient(app_module.app)
    client.post("/rules", json={"keyword": "PRICE", "dm_message": "Here you go"})

    assert post_signed_webhook(client, comment_payload()).status_code == 200

    assert client.get("/stats").json() == {
        "sent": 0,
        "failed": 0,
        "queued": 1,
        "duplicates_blocked": 0,
    }


def test_stale_claimed_webhook_job_is_recovered(tmp_path, monkeypatch):
    app_module = load_app(tmp_path, monkeypatch)
    client = TestClient(app_module.app)
    client.post("/rules", json={"keyword": "PRICE", "dm_message": "Here you go"})
    post_signed_webhook(client, comment_payload())

    with app_module.db() as conn:
        conn.execute(
            "UPDATE webhook_jobs SET processed = ?, claimed_at = 0",
            (app_module.JOB_CLAIMED,),
        )

    assert app_module.recover_stale_webhook_jobs() == 1
    assert app_module.process_webhook_jobs() == 1
    assert client.get("/stats").json()["queued"] == 1


def test_comment_deleted_before_created_does_not_queue_delivery(tmp_path, monkeypatch):
    app_module = load_app(tmp_path, monkeypatch)
    client = TestClient(app_module.app)
    client.post("/rules", json={"keyword": "PRICE", "dm_message": "Here you go"})

    deleted = comment_payload(event_id="evt_delete", event_type="comment.deleted")
    assert post_signed_webhook(client, deleted).status_code == 200
    app_module.process_webhook_jobs()

    created = comment_payload(event_id="evt_create")
    assert post_signed_webhook(client, created).status_code == 200
    app_module.process_webhook_jobs()

    assert client.get("/stats").json() == {
        "sent": 0,
        "failed": 0,
        "queued": 0,
        "duplicates_blocked": 0,
    }


def test_comment_deleted_cancels_queued_delivery(tmp_path, monkeypatch):
    app_module = load_app(tmp_path, monkeypatch)
    client = TestClient(app_module.app)
    client.post("/rules", json={"keyword": "PRICE", "dm_message": "Here you go"})

    assert post_signed_webhook(client, comment_payload()).status_code == 200
    app_module.process_webhook_jobs()
    assert client.get("/stats").json()["queued"] == 1

    deleted = comment_payload(event_id="evt_delete", event_type="comment.deleted")
    assert post_signed_webhook(client, deleted).status_code == 200
    app_module.process_webhook_jobs()

    assert client.get("/stats").json()["queued"] == 0


def test_send_and_reconcile_delivered_dm(tmp_path, monkeypatch):
    app_module = load_app(tmp_path, monkeypatch)
    client = TestClient(app_module.app)
    client.post("/rules", json={"keyword": "PRICE", "dm_message": "Here you go"})
    post_signed_webhook(client, comment_payload())
    app_module.process_webhook_jobs()

    FakePseudoGramClient.post_responses = [FakeResponse(202, {"dm_id": "dm_1", "status": "queued"})]
    FakePseudoGramClient.get_responses = [FakeResponse(200, {"dm_id": "dm_1", "status": "delivered"})]
    monkeypatch.setattr(app_module.httpx, "AsyncClient", FakePseudoGramClient)

    asyncio.run(app_module.send_due_deliveries())
    with app_module.db() as conn:
        conn.execute("UPDATE deliveries SET next_attempt_at = 0 WHERE dm_id = 'dm_1'")
    asyncio.run(app_module.reconcile_accepted_deliveries())

    assert client.get("/stats").json() == {
        "sent": 1,
        "failed": 0,
        "queued": 0,
        "duplicates_blocked": 0,
    }


def test_accepted_failed_dm_is_requeued_for_retry(tmp_path, monkeypatch):
    app_module = load_app(tmp_path, monkeypatch)
    client = TestClient(app_module.app)
    client.post("/rules", json={"keyword": "PRICE", "dm_message": "Here you go"})
    post_signed_webhook(client, comment_payload())
    app_module.process_webhook_jobs()

    FakePseudoGramClient.post_responses = [FakeResponse(202, {"dm_id": "dm_1", "status": "queued"})]
    FakePseudoGramClient.get_responses = [FakeResponse(200, {"dm_id": "dm_1", "status": "failed"})]
    monkeypatch.setattr(app_module.httpx, "AsyncClient", FakePseudoGramClient)

    asyncio.run(app_module.send_due_deliveries())
    with app_module.db() as conn:
        conn.execute("UPDATE deliveries SET next_attempt_at = 0 WHERE dm_id = 'dm_1'")
    asyncio.run(app_module.reconcile_accepted_deliveries())

    stats = client.get("/stats").json()
    assert stats["queued"] == 1
    with app_module.db() as conn:
        delivery = conn.execute("SELECT * FROM deliveries").fetchone()
    assert delivery["dm_id"] is None
    assert delivery["send_generation"] == 1


def test_stale_sending_delivery_is_recovered(tmp_path, monkeypatch):
    app_module = load_app(tmp_path, monkeypatch)
    client = TestClient(app_module.app)
    client.post("/rules", json={"keyword": "PRICE", "dm_message": "Here you go"})
    post_signed_webhook(client, comment_payload())
    app_module.process_webhook_jobs()

    with app_module.db() as conn:
        conn.execute(
            "UPDATE deliveries SET status = 'sending', locked_at = 0, updated_at = ?",
            (app_module.utc_now_iso(),),
        )

    assert app_module.recover_stale_sending() == 1
    assert client.get("/stats").json()["queued"] == 1
