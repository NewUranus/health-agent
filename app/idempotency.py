"""登录模式下的写请求幂等；未提供幂等键时保持原行为。"""
import asyncio
import json

from app.auth import digest


def actor_id(request):
    user = getattr(request.state, "user", None)
    if user:
        return user["id"]
    if getattr(request.state, "admin_auth", None) == "bearer":
        return "bearer-admin"
    return "admin"


def payload_hash(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


async def idempotent(request, scope, payload, producer):
    if not request.app.state.auth_required:
        return await producer()
    key = request.headers.get("idempotency-key")
    if not key:
        return await producer()
    store = request.app.state.task_store
    actor = actor_id(request)
    hashed = payload_hash(payload)
    prior = await asyncio.to_thread(store.reserve_write, actor, scope, key, hashed)
    if prior is not None:
        return prior
    try:
        result = await producer()
    except BaseException:
        await asyncio.to_thread(store.release_write, actor, scope, key)
        raise
    await asyncio.to_thread(store.complete_write, actor, scope, key, result)
    return result
