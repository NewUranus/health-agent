"""运行告警故障注入验收。"""
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.operations import snapshot
from app.tasks import TaskStore
from test_foundation import secured


def test_no_worker_queue_delay_and_stale_lease_alerts(secured, monkeypatch):
    _, database, users = secured
    store = TaskStore(database)
    monkeypatch.setenv("HEALTH_AGENT_QUEUE_ALERT_SECONDS", "1")
    pending = store.submit(users["alice"]["id"], "alert-pending-key", {
        "thread_id": "alert-pending", "message": "仅用于告警故障注入",
    }, "alert-pending-request")[0]
    running = store.submit(users["bob"]["id"], "alert-running-key", {
        "thread_id": "alert-running", "message": "仅用于告警故障注入",
    }, "alert-running-request")[0]
    claimed = store.claim("alert-worker")
    assert claimed and claimed["id"] in {pending["id"], running["id"]}
    with database.connection() as execute:
        execute("UPDATE hp_tasks SET created_at = ? WHERE status = 'pending'", ((int(time.time()) - 5) * 1_000_000_000,))
        execute("UPDATE hp_tasks SET lease_until = ? WHERE status = 'running'", (int(time.time()) - 5,))
        execute("DELETE FROM hp_workers")
    codes = {alert["code"] for alert in snapshot(database)["alerts"]}
    assert {"NO_WORKER", "QUEUE_DELAY", "STALE_LEASE"} <= codes
