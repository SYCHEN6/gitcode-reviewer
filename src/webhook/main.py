"""FastAPI Webhook Gateway — 接收 GitCode 事件并路由到对应处理器。

端点：
    POST /webhook  — 接收所有 GitCode Webhook 事件
    GET  /health   — 健康检查
"""

import asyncio
import logging

import redis.asyncio as aioredis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from src.config import settings
from src.webhook.handlers import handle_merge_request, handle_note, handle_push

logger = logging.getLogger(__name__)

app = FastAPI(title="gitcode-reviewer webhook")

_redis: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    token = request.headers.get("X-Gitcode-Token")
    if token != settings.WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = payload.get("object_kind")
    logger.info("Webhook received: event_type=%s", event_type)
    redis = _get_redis()

    if event_type == "merge_request":
        action = payload.get("object_attributes", {}).get("action", "")
        logger.info("merge_request action=%s", action)
        if action in ("open", "update", "reopen"):
            await handle_merge_request(payload, background_tasks, redis)
        else:
            logger.info("merge_request action=%r ignored", action)
    elif event_type == "note":
        await handle_note(payload, background_tasks, redis)
    elif event_type == "push":
        await handle_push(payload, background_tasks)
    else:
        logger.warning("Unknown event_type=%r, full keys: %s", event_type, list(payload.keys()))

    return JSONResponse({"status": "accepted"}, status_code=202)
