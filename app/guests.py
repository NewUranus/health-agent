"""免登录体验：临时游客账号、使用额度与到期清理。

游客是服务端签发会话的临时账号，数据命名空间与普通账号一样隔离；
不能用密码登录，不能被管理员改成其他角色，到期后连同健康记录一起删除。
额度在提交任务的同一事务内扣减，提交失败时一并回滚，幂等重放不重复扣减。
"""
import os
import time
import uuid
from ipaddress import ip_address, ip_network

from app.auth import GUEST_PASSWORD, GUEST_PREFIX, GUEST_ROLE, AuthFailure, digest
DEFAULT_TRUSTED = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"
# 游客到期后多保留一小时再清理，避免和仍在收尾的任务冲突
PURGE_GRACE = 3600
BOUNDS = {"HEALTH_AGENT_GUEST_MESSAGES": (20, 1, 1000), "HEALTH_AGENT_GUEST_DAILY_MESSAGES": (200, 0, 1000000),
          "HEALTH_AGENT_GUEST_PER_IP": (5, 1, 1000), "HEALTH_AGENT_GUEST_HOURS": (24, 1, 168)}
GUEST_BLOCKED = {"/chat", "/conversations", "/auth/password"}
GUEST_BLOCKED_PREFIXES = ("/tools/", "/conversations/")


def policy():
    flag = os.getenv("HEALTH_AGENT_GUEST", "1")
    if flag not in {"0", "1"}:
        raise ValueError("HEALTH_AGENT_GUEST 只能为 0 或 1。")
    result = {"enabled": flag == "1"}
    for key, (default, low, high) in BOUNDS.items():
        try:
            value = int(os.getenv(key, str(default)))
            if not low <= value <= high:
                raise ValueError()
        except ValueError:
            raise ValueError(f"{key} 须为 {low}–{high} 的整数。") from None
        result[key] = value
    return result


def guest_blocked(resource):
    """游客不能使用的高级功能；健康对话、随访记录和自己的任务状态仍可用。"""
    return bool(resource) and (resource in GUEST_BLOCKED or resource.startswith(GUEST_BLOCKED_PREFIXES))


def login_required(feature):
    return AuthFailure(403, "LOGIN_REQUIRED", f"{feature}需要登录账号，免登录体验不提供。")


def _trusted(value, networks):
    try:
        address = ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in networks)


def client_ip(request):
    """只有直连方是受信任的反向代理时才读取 X-Forwarded-For，取最右侧的非代理地址。"""
    peer = request.client.host if request.client else "unknown"
    try:
        networks = [ip_network(item.strip(), strict=False) for item in os.getenv("HEALTH_AGENT_TRUSTED_PROXIES", DEFAULT_TRUSTED).split(",") if item.strip()]
    except ValueError:
        raise ValueError("HEALTH_AGENT_TRUSTED_PROXIES 须为逗号分隔的 IP 或网段。") from None
    if not _trusted(peer, networks):
        return peer
    forwarded = [item.strip() for item in request.headers.get("x-forwarded-for", "").split(",") if item.strip()]
    for candidate in reversed(forwarded):
        try:
            ip_address(candidate)
        except ValueError:
            return peer
        if not _trusted(candidate, networks):
            return candidate
    return forwarded[0] if forwarded else peer


def day_key(now):
    # 按北京时间计日，和使用者理解的“今天”一致
    return time.strftime("%Y-%m-%d", time.gmtime(now + 8 * 3600))


def _paused(execute):
    row = execute("SELECT value FROM hp_system_settings WHERE name = 'guest_paused'").fetchone()
    return bool(row and row["value"] == "1")


def _bump(execute, scope, until_at):
    return execute("INSERT INTO hp_guest_usage VALUES (?, 1, ?) ON CONFLICT (scope) DO UPDATE SET used = hp_guest_usage.used + 1 RETURNING used",
                   (scope, until_at)).fetchone()["used"]


def _used(execute, scope):
    row = execute("SELECT used FROM hp_guest_usage WHERE scope = ?", (scope,)).fetchone()
    return row["used"] if row else 0


def availability(database):
    """首页是否可以直接进入免登录体验。"""
    config = policy()
    if not config["enabled"] or config["HEALTH_AGENT_GUEST_DAILY_MESSAGES"] == 0:
        return False
    with database.connection() as execute:
        if _paused(execute):
            return False
        return _used(execute, "guest-day:" + day_key(int(time.time()))) < config["HEALTH_AGENT_GUEST_DAILY_MESSAGES"]


def create(database, client, previous_token, request_id):
    """签发游客会话；同一网络每天可创建的游客数有限。调用方负责写 Cookie。"""
    import secrets
    config = policy()
    if not config["enabled"]:
        raise AuthFailure(403, "GUEST_DISABLED", "免登录体验未开启，请登录后使用。")
    now = int(time.time())
    hours = config["HEALTH_AGENT_GUEST_HOURS"]
    user_id = uuid.uuid4().hex
    user = {"id": user_id, "username": GUEST_PREFIX + user_id[:12], "role": GUEST_ROLE}
    token, expires = secrets.token_urlsafe(32), now + hours * 3600
    with database.connection() as execute:
        if _paused(execute):
            raise AuthFailure(503, "GUEST_PAUSED", "免登录体验暂时关闭，请登录后使用。")
        execute("DELETE FROM hp_guest_usage WHERE until_at <= ?", (now,))
        if _bump(execute, "guest-ip:" + digest(client), now + 86400) > config["HEALTH_AGENT_GUEST_PER_IP"]:
            raise AuthFailure(429, "GUEST_LIMITED", "当前网络今天开启免登录体验的次数已达上限，请登录后使用。")
        execute("DELETE FROM hp_sessions WHERE token_hash = ?", (digest(previous_token or ""),))
        execute("INSERT INTO hp_users (id, username, password_hash, created_at, role) VALUES (?, ?, ?, ?, ?)",
                (user_id, user["username"], GUEST_PASSWORD, now, GUEST_ROLE))
        execute("INSERT INTO hp_sessions VALUES (?, ?, ?)", (digest(token), user_id, expires))
        database._audit(execute, user_id, "guest.created", "/auth/guest", request_id, 201)
    return token, user, expires


def consume_message(execute, user_id):
    """在提交任务的事务里扣减额度；抛错时整个提交回滚，额度不被占用。"""
    config = policy()
    if not config["enabled"]:
        raise AuthFailure(403, "GUEST_DISABLED", "免登录体验已关闭，请登录后使用。")
    if _paused(execute):
        raise AuthFailure(503, "GUEST_PAUSED", "免登录体验暂时关闭，请登录后使用。")
    now = int(time.time())
    execute("DELETE FROM hp_guest_usage WHERE until_at <= ?", (now,))
    limit = config["HEALTH_AGENT_GUEST_MESSAGES"]
    if _bump(execute, "guest-user:" + user_id, now + config["HEALTH_AGENT_GUEST_HOURS"] * 3600 + PURGE_GRACE) > limit:
        raise AuthFailure(429, "GUEST_QUOTA_EXHAUSTED", f"免登录体验的 {limit} 条消息已用完，登录后可继续使用。")
    if _bump(execute, "guest-day:" + day_key(now), now + 2 * 86400) > config["HEALTH_AGENT_GUEST_DAILY_MESSAGES"]:
        raise AuthFailure(429, "GUEST_DAILY_EXHAUSTED", "今天的免登录体验名额已用完，请登录后使用或明天再来。")


def quota(database, user_id):
    config = policy()
    with database.connection() as execute:
        used = _used(execute, "guest-user:" + user_id)
        daily = _used(execute, "guest-day:" + day_key(int(time.time())))
        paused = _paused(execute)
    limit = config["HEALTH_AGENT_GUEST_MESSAGES"]
    return {"limit": limit, "remaining": max(0, limit - used), "hours": config["HEALTH_AGENT_GUEST_HOURS"],
            "daily_available": config["enabled"] and not paused and daily < config["HEALTH_AGENT_GUEST_DAILY_MESSAGES"]}


def snapshot(execute):
    """运维面板：当前游客数、今日已用消息与上限、是否暂停。"""
    config = policy()
    now = int(time.time())
    active = execute("SELECT COUNT(*) AS count FROM hp_users WHERE role = ? AND created_at > ?",
                     (GUEST_ROLE, now - config["HEALTH_AGENT_GUEST_HOURS"] * 3600)).fetchone()["count"]
    return {"enabled": config["enabled"], "paused": _paused(execute), "active": active,
            "messages_today": _used(execute, "guest-day:" + day_key(now)), "daily_limit": config["HEALTH_AGENT_GUEST_DAILY_MESSAGES"],
            "per_guest_limit": config["HEALTH_AGENT_GUEST_MESSAGES"], "hours": config["HEALTH_AGENT_GUEST_HOURS"]}


def set_paused(database, paused, request_id):
    with database.connection() as execute:
        execute("INSERT INTO hp_system_settings VALUES ('guest_paused', ?) ON CONFLICT (name) DO UPDATE SET value = excluded.value",
                ("1" if paused else "0",))
        database._audit(execute, "knowledge-admin", "guest.paused" if paused else "guest.resumed", "guest", request_id, 200)


TASK_TABLES = ("hp_task_events", "hp_task_traces", "hp_task_effects", "hp_task_checkpoints", "hp_task_priorities", "hp_task_reviews")


async def purge_expired(database, store=None, checkpointer=None, now=None, limit=200):
    """删除到期游客的全部数据：会话、任务与事件、会话索引、模型用量、长期记忆和检查点。

    先删图数据再删数据库行；中途失败时账号行仍在，下次清理会重试。审计记录保留。
    """
    import asyncio
    config = policy()
    now = int(time.time()) if now is None else now
    cutoff = now - config["HEALTH_AGENT_GUEST_HOURS"] * 3600 - PURGE_GRACE

    def expired():
        with database.connection() as execute:
            rows = execute("SELECT id FROM hp_users WHERE role = ? AND created_at < ? ORDER BY created_at LIMIT ?",
                           (GUEST_ROLE, cutoff, limit)).fetchall()
            result = []
            for row in rows:
                active = execute("SELECT COUNT(*) AS count FROM hp_tasks WHERE user_id = ? AND status IN ('pending', 'running')", (row["id"],)).fetchone()["count"]
                if not active:
                    threads = [item["thread_id"] for item in execute("SELECT DISTINCT thread_id FROM hp_tasks WHERE user_id = ?", (row["id"],)).fetchall()]
                    result.append((row["id"], threads))
            return result

    def remove(user_id):
        with database.connection() as execute:
            owned = "SELECT id FROM hp_tasks WHERE user_id = ?"
            for table in TASK_TABLES:
                execute(f"DELETE FROM {table} WHERE task_id IN ({owned})", (user_id,))
            for table in ("hp_task_slots", "hp_tasks", "hp_write_idempotency", "hp_conversations", "hp_threads", "hp_sessions"):
                execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
            execute("DELETE FROM hp_model_costs WHERE usage_id IN (SELECT id FROM hp_model_usage WHERE user_id = ?)", (user_id,))
            execute("DELETE FROM hp_model_usage WHERE user_id = ?", (user_id,))
            execute("DELETE FROM hp_guest_usage WHERE scope = ?", ("guest-user:" + user_id,))
            execute("DELETE FROM hp_users WHERE id = ? AND role = ?", (user_id, GUEST_ROLE))
            database._audit(execute, None, "guest.purged", user_id, uuid.uuid4().hex, 200)

    purged = 0
    for user_id, threads in await asyncio.to_thread(expired):
        if store is not None:
            for key in ("profile", "history", "followup"):
                await store.adelete(("users", "account:" + user_id), key)
        if checkpointer is not None:
            for thread_id in threads:
                await checkpointer.adelete_thread("auth-" + digest(user_id + ":" + thread_id))
        await asyncio.to_thread(remove, user_id)
        purged += 1
    return purged
