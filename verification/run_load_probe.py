"""对隔离 PostgreSQL 的任务接纳和限流做有界并发探针。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import statistics
import time
import uuid

from app.auth import AuthFailure, AuthStore
from app.tasks import TaskStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=160)
    parser.add_argument("--concurrency", type=int, default=24)
    parser.add_argument("--users", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.requests <= 5000 or not 1 <= args.concurrency <= 200 or not 1 <= args.users <= 500:
        parser.error("探针参数超出安全范围。")
    database = AuthStore()
    database.setup()
    store = TaskStore(database)
    store.setup()
    prefix = "load" + uuid.uuid4().hex[:8]
    users = [database.provision(f"{prefix}{index}", uuid.uuid4().hex) for index in range(args.users)]

    def submit(index):
        started = time.perf_counter()
        try:
            task, created = store.submit(users[index % len(users)]["id"], f"load-key-{index:06d}", {
                "thread_id": f"load-thread-{index}", "message": "有界任务接纳压测",
            }, f"load-request-{index}")
            outcome = "accepted" if created and task["status"] == "pending" else "replayed"
        except AuthFailure as exc:
            outcome = exc.code
        return outcome, (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(submit, range(args.requests)))
    elapsed = time.perf_counter() - started
    counts = {}
    for outcome, _ in results:
        counts[outcome] = counts.get(outcome, 0) + 1
    durations = sorted(duration for _, duration in results)
    p95 = durations[min(len(durations) - 1, int(len(durations) * 0.95))]
    print({
        "requests": args.requests, "concurrency": args.concurrency, "users": args.users,
        "elapsed_seconds": round(elapsed, 3), "throughput_per_second": round(args.requests / elapsed, 1),
        "latency_ms_p50": round(statistics.median(durations), 2), "latency_ms_p95": round(p95, 2),
        "outcomes": counts,
    })
    if counts.get("accepted", 0) > store.queue_limit or set(counts) - {"accepted", "QUEUE_CAPACITY"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
