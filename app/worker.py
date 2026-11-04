"""独立消费数据库任务队列；必须与 API 使用同一 PostgreSQL。"""
import asyncio
import os
from dotenv import load_dotenv

from app.agent import HealthAgent
from app.auth import AuthStore, ROOT
from app.tasks import TaskRunner, TaskStore
from app.runtime_config import settings, record
from app.telemetry import runtime as telemetry_runtime


async def main():
    load_dotenv(ROOT / ".env")
    if not os.getenv("DATABASE_URL"):
        raise SystemExit("独立 Worker 需要 DATABASE_URL，内存健康记忆不能跨进程共享。")
    timeout = int(os.getenv("HEALTH_AGENT_TASK_TIMEOUT", "300"))
    if not 1 <= timeout <= 3600:
        raise SystemExit("HEALTH_AGENT_TASK_TIMEOUT 须为 1–3600 秒。")
    with telemetry_runtime("worker"):
        config = settings()
        database = AuthStore()
        await asyncio.to_thread(database.setup)
        tasks = TaskStore(database)
        await asyncio.to_thread(tasks.setup)
        await asyncio.to_thread(record, database, config)
        async with HealthAgent(mock=os.getenv("HEALTH_AGENT_MOCK") == "1") as agent:
            runner = TaskRunner(tasks, agent, workers=config["HEALTH_AGENT_TASK_WORKERS"], timeout=timeout)
            await runner.start()
            try:
                await asyncio.Event().wait()
            finally:
                await runner.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
