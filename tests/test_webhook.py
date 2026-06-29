"""Webhook 端点测试。"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

from src.webhook.main import app

client = TestClient(app)

MR_PAYLOAD = {
    "object_kind": "merge_request",
    "project": {"id": 1, "path_with_namespace": "owner/repo"},
    "object_attributes": {
        "iid": 42,
        "action": "open",
        "last_commit": {"id": "abc123"},
    },
}


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_webhook_rejects_missing_token():
    r = client.post("/webhook", json=MR_PAYLOAD)
    assert r.status_code == 401


def test_webhook_rejects_wrong_token():
    r = client.post("/webhook", json=MR_PAYLOAD, headers={"X-Gitcode-Token": "wrong"})
    assert r.status_code == 401


@patch("src.webhook.handlers.run_review_graph", new_callable=AsyncMock)
@patch("src.webhook.main._get_redis")
def test_webhook_accepts_merge_request(mock_redis_factory, mock_review, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "test-secret")

    mock_redis = AsyncMock()
    mock_redis.exists = AsyncMock(return_value=0)
    mock_redis.setex = AsyncMock()
    mock_redis_factory.return_value = mock_redis

    from src.config import settings
    settings.WEBHOOK_SECRET = "test-secret"

    r = client.post(
        "/webhook",
        json=MR_PAYLOAD,
        headers={"X-Gitcode-Token": "test-secret"},
    )
    assert r.status_code == 202
    assert r.json()["status"] == "accepted"


@patch("src.webhook.handlers.run_review_graph", new_callable=AsyncMock)
@patch("src.webhook.main._get_redis")
def test_webhook_deduplicates_same_commit(mock_redis_factory, mock_review):
    mock_redis = AsyncMock()
    mock_redis.exists = AsyncMock(return_value=1)  # key already exists
    mock_redis.setex = AsyncMock()
    mock_redis_factory.return_value = mock_redis

    from src.config import settings
    settings.WEBHOOK_SECRET = "test-secret"

    r = client.post(
        "/webhook",
        json=MR_PAYLOAD,
        headers={"X-Gitcode-Token": "test-secret"},
    )
    assert r.status_code == 202
    mock_review.assert_not_called()
