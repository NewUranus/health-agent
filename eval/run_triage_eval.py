"""急症分诊评测：对比纯规则与“规则 + 模型”双通道的召回率和误报率。

双通道召回率低于纯规则时以非零状态退出，不能上线。
Mock 模式不调用模型，双通道结果等同纯规则，只用于检查脚本本身。
"""
import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from app.graph import Nodes
from app.llm import make_llm
from app.model_gateway import model_run
from app.safety import check_red_flags

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "eval/emergency_cases.jsonl"


def rate(hit, total):
    return round(hit / total, 4) if total else None


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * fraction)))], 3)


def summarize(cases, key):
    positives = [case for case in cases if case["expect"]]
    negatives = [case for case in cases if not case["expect"]]
    return {"recall": rate(sum(case[key] for case in positives), len(positives)),
            "false_positive_rate": rate(sum(case[key] for case in negatives), len(negatives)),
            "missed": [case["text"] for case in positives if not case[key]],
            "false_alarms": [case["text"] for case in negatives if case[key]]}


async def run(mock=False, concurrency=4):
    load_dotenv(ROOT / ".env")
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    llm = make_llm(mock)
    if not mock and llm is None:
        raise SystemExit("真实模式需要配置 QWEN_API_KEY；离线检查请加 --mock。")
    nodes = Nodes(None, None, None, llm, mock)
    gate = asyncio.Semaphore(concurrency)

    async def judge(case):
        async with gate:
            started = time.perf_counter()
            # 每条用例单独计算调用预算，和线上每轮一致
            with model_run():
                verdict = await nodes.triage(case["text"])
            elapsed = time.perf_counter() - started
        rule = bool(check_red_flags(case["text"]))
        return {**case, "rule": rule, "dual": bool(verdict.flags), "source": verdict.source,
                "classifier": verdict.classifier, "evidence": verdict.evidence, "category_actual": list(verdict.flags),
                "seconds": round(elapsed, 3) if verdict.source != "rule" else None}

    results = await asyncio.gather(*(judge(case) for case in cases))
    rule, dual = summarize(results, "rule"), summarize(results, "dual")
    model_seconds = [case["seconds"] for case in results if case["seconds"] is not None and case["classifier"] != "skipped"]
    report = {"at": datetime.now(timezone.utc).isoformat(), "mode": "mock" if mock else "real", "cases": len(results),
              "positives": sum(case["expect"] for case in results), "negatives": sum(not case["expect"] for case in results),
              "rule": rule, "dual": dual,
              "model_added_true_positives": sum(case["expect"] and case["dual"] and not case["rule"] for case in results),
              "model_added_false_positives": sum(not case["expect"] and case["dual"] and not case["rule"] for case in results),
              "classifier": {status: sum(case["classifier"] == status for case in results) for status in ("not_needed", "ok", "failed", "invalid", "skipped")},
              "model_latency_seconds": {"p50": percentile(model_seconds, .5), "p95": percentile(model_seconds, .95), "calls": len(model_seconds)},
              "details": results}
    directory = ROOT / "eval/results"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("triage-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"用例：{report['cases']}（急症 {report['positives']}，非急症 {report['negatives']}），模式：{report['mode']}")
    print(f"纯规则：召回 {rule['recall']:.1%}，误报 {rule['false_positive_rate']:.1%}")
    print(f"双通道：召回 {dual['recall']:.1%}，误报 {dual['false_positive_rate']:.1%}")
    print(f"模型新增：正确识别 {report['model_added_true_positives']} 条，误报 {report['model_added_false_positives']} 条")
    print("模型通道状态：" + json.dumps(report["classifier"], ensure_ascii=False))
    print("模型分诊耗时（秒）：" + json.dumps(report["model_latency_seconds"], ensure_ascii=False))
    print(f"结果：{path}")
    return report


def main():
    parser = argparse.ArgumentParser(description="急症分诊评测")
    parser.add_argument("--mock", action="store_true", help="不调用模型，只检查脚本和规则通道")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    report = asyncio.run(run(args.mock, args.concurrency))
    if report["dual"]["recall"] < report["rule"]["recall"]:
        print("双通道召回率低于纯规则，不能上线。")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
