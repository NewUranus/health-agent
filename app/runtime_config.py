"""启动时校验非秘密运行配置，并保存可追溯的配置快照。"""
import hashlib
import json
import os
import time
from app.model_pricing import prices


def settings():
    result = {}
    bounds = {"HEALTH_AGENT_TASK_WORKERS": (2, 1, 32), "HEALTH_AGENT_QUEUE_LIMIT": (200, 1, 100000),
        "HEALTH_AGENT_USER_QUEUE_LIMIT": (8, 1, 1000), "HEALTH_AGENT_QUEUE_ALERT_SECONDS": (60, 1, 86400)}
    for key, (default, low, high) in bounds.items():
        try:
            value = int(os.getenv(key, str(default)))
            if not low <= value <= high:
                raise ValueError()
        except ValueError:
            raise ValueError(f"{key} 须为 {low}–{high} 的整数。") from None
        result[key] = value
    prices()
    result["release"] = os.getenv("HEALTH_AGENT_RELEASE", "local")[:200]
    return result


def record(database, config):
    raw = json.dumps(config, sort_keys=True)
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()
    with database.connection() as execute:
        execute("INSERT INTO hp_config_snapshots VALUES (?, ?, ?) ON CONFLICT (id) DO NOTHING", (fingerprint, int(time.time()), raw))
