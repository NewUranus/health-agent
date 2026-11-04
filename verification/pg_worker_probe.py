"""仅供 PostgreSQL 故障验收：在事务提交前后阻塞，供测试终止进程。"""
import asyncio
from pathlib import Path
import sys

import app.graph
import app.memory
import app.tasks
from app.worker import main

mode, marker = sys.argv[1:]
app.tasks.LEASE_SECONDS = 2
if mode == "before_commit":
    original = app.memory._write_assessment
    async def blocked(*args, **kwargs):
        await original(*args, **kwargs)
        Path(marker).touch()
        await asyncio.sleep(120)
    app.memory._write_assessment = blocked
elif mode == "after_commit":
    original = app.graph.save_assessment
    async def blocked(*args, **kwargs):
        await original(*args, **kwargs)
        Path(marker).touch()
        await asyncio.sleep(120)
    app.graph.save_assessment = blocked
elif mode == "ready":
    original = app.tasks.TaskRunner.start
    async def ready(self):
        await original(self)
        Path(marker).touch()
    app.tasks.TaskRunner.start = ready
else:
    raise SystemExit("未知故障注入阶段")

asyncio.run(main())
