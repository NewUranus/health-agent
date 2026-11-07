"""事实支持校验评测：以“不支持”为正类，统计判定的精确率和召回率。

用例在 eval/grounding_cases.jsonl：资料取自 corpus/，回答逐句人工标注是否有资料支持，
一半用例故意掺入资料以外的说法。Mock 模式不调用判定模型，只检查用例能否与拆句结果对齐。
"""
import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from app.graph import Nodes
from app.grounding import split_claims
from app.llm import make_llm
from app.model_gateway import model_run
from app.rag import Retriever

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "eval/grounding_cases.jsonl"
TARGET_RECALL = 0.8


def rate(hit, total):
    return round(hit / total, 4) if total else None


def align(case):
    """每个参与判定的句子都必须有标注，否则是用例或拆句规则出了问题。"""
    claims = [claim for claim in split_claims(case["answer"]) if not claim.meta]
    labels, used = {}, set()
    for claim in claims:
        core = claim.text.rstrip("。；;！？!?")
        # 一条标注可能被分号拆成几句，几句共用这条标注
        matches = [(text, supported) for text, supported in case["labels"] if text in core or core in text]
        if len(matches) != 1:
            raise SystemExit(f"用例句子无法对齐标注：{claim.text}")
        labels[claim.index] = matches[0][1]
        used.add(matches[0][0])
    if len(used) != len(case["labels"]):
        raise SystemExit(f"有标注没有对应的句子：{case['answer'][:40]}")
    return labels


async def run(mock=False, concurrency=4):
    load_dotenv(ROOT / ".env")
    corpus = {chunk.id: {"id": chunk.id, "title": chunk.title, "text": chunk.text} for chunk in Retriever.from_dir(ROOT / "corpus").chunks}
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    aligned = [align(case) for case in cases]
    llm = None if mock else make_llm(False)
    if not mock and llm is None:
        raise SystemExit("真实模式需要配置 QWEN_API_KEY；离线检查请加 --mock。")
    nodes = Nodes(None, None, None, llm, mock, judge=llm)
    gate = asyncio.Semaphore(concurrency)

    async def judge(case, labels):
        async with gate:
            started = time.perf_counter()
            with model_run():
                result = await nodes.ground(case["answer"], [corpus[cid] for cid in case["chunks"]], case.get("tool_results"))
            seconds = time.perf_counter() - started
        flagged = {claim.index for claim in result.unsupported}
        return {"answer": case["answer"], "skipped": result.skipped, "seconds": round(seconds, 3),
                "sentences": [{"index": index, "supported": supported, "flagged": index in flagged, "reason": result.reasons.get(index, "")}
                              for index, supported in labels.items()]}

    results = await asyncio.gather(*(judge(case, labels) for case, labels in zip(cases, aligned)))
    sentences = [row for case in results if not case["skipped"] for row in case["sentences"]]
    true_positive = sum(row["flagged"] and not row["supported"] for row in sentences)
    false_positive = sum(row["flagged"] and row["supported"] for row in sentences)
    positives = sum(not row["supported"] for row in sentences)
    seconds = sorted(case["seconds"] for case in results if not case["skipped"])
    report = {"at": datetime.now(timezone.utc).isoformat(), "mode": "mock" if mock else "real", "cases": len(results),
              "sentences": sum(len(labels) for labels in aligned), "skipped_cases": sum(bool(case["skipped"]) for case in results),
              "unsupported_precision": rate(true_positive, true_positive + false_positive),
              "unsupported_recall": rate(true_positive, positives),
              "supported_wrongly_flagged": false_positive,
              "missed": [row | {"answer": case["answer"]} for case in results for row in case["sentences"] if not row["supported"] and not row["flagged"] and not case["skipped"]],
              "false_flags": [row | {"answer": case["answer"]} for case in results for row in case["sentences"] if row["supported"] and row["flagged"] and not case["skipped"]],
              "judge_seconds": {"p50": seconds[len(seconds) // 2] if seconds else None, "p95": seconds[int(len(seconds) * .95)] if seconds else None},
              "details": results}
    directory = ROOT / "eval/results"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("grounding-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"用例：{report['cases']}，标注句子：{report['sentences']}，模式：{report['mode']}，跳过：{report['skipped_cases']}")
    if report["skipped_cases"] == report["cases"]:
        print("判定模型未调用，只检查了用例与拆句对齐。")
    else:
        print(f"“不支持”判定：精确率 {report['unsupported_precision']:.1%}，召回率 {report['unsupported_recall']:.1%}，误判有支持的句子 {false_positive} 句")
        print("每次判定耗时（秒）：" + json.dumps(report["judge_seconds"], ensure_ascii=False))
    print(f"结果：{path}")
    return report


QUESTIONS = ["高血压孕妇能吃什么药", "每天吃多少盐合适", "喝红酒对心脏好吗", "怎么正确测血压", "胆固醇5.8要吃药吗",
             "戒烟后多久风险会下降", "中风有什么早期信号", "运动多少合适，老年人呢", "BMI 26算胖吗", "高血压能不能根治",
             "降压药可以自己停吗", "儿童血压正常值是多少"]


class CorpusKB:
    def __init__(self):
        self.retriever = Retriever.from_dir(ROOT / "corpus")

    async def search(self, query, k=4, min_similarity=None):
        return self.retriever.search(query, k)


class NoTools:
    async def list_tools(self):
        return []


async def run_qa():
    """真实问答抽查：统计初稿和最终回答中含不支持句子的比例、调用次数与耗时。判定方与校验方是同一模型。"""
    load_dotenv(ROOT / ".env")
    llm = make_llm(False)
    if llm is None:
        raise SystemExit("问答抽查需要配置 QWEN_API_KEY。")
    nodes = Nodes(NoTools(), None, CorpusKB(), llm, False, judge=llm)
    rows = []
    for question in QUESTIONS:
        started = time.perf_counter()
        with model_run() as usage:
            update = await nodes.answer({"text": question, "profile": {}, "messages": []})
        data = update["event_data"]
        grounding = data.get("grounding") or {}
        first = grounding.get("initial_unsupported", grounding.get("unsupported", []))
        rows.append({"question": question, "seconds": round(time.perf_counter() - started, 2), "model_calls": usage.calls,
                     "skipped": data.get("grounding_skip_reason"), "draft_unsupported": first,
                     "rewritten": grounding.get("rewritten", False), "removed": grounding.get("removed", 0),
                     "refused": data.get("grounding_refused", False)})
    checked = [row for row in rows if not row["skipped"]]
    seconds = sorted(row["seconds"] for row in checked)
    report = {"at": datetime.now(timezone.utc).isoformat(), "questions": len(rows), "skipped": len(rows) - len(checked),
              "draft_with_unsupported": sum(bool(row["draft_unsupported"]) for row in checked),
              "rewritten": sum(row["rewritten"] for row in checked), "answers_with_removal": sum(bool(row["removed"]) for row in checked),
              "refused": sum(row["refused"] for row in checked),
              "model_calls_mean": round(sum(row["model_calls"] for row in checked) / len(checked), 2) if checked else None,
              "seconds": {"p50": seconds[len(seconds) // 2] if seconds else None, "max": seconds[-1] if seconds else None}, "rows": rows}
    directory = ROOT / "eval/results"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("grounding-qa-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"问题：{report['questions']}，初稿含不支持句子：{report['draft_with_unsupported']}，重写：{report['rewritten']}，"
          f"最终删句：{report['answers_with_removal']}，拒答：{report['refused']}")
    print(f"平均模型调用：{report['model_calls_mean']} 次；耗时（秒）：" + json.dumps(report["seconds"], ensure_ascii=False))
    print(f"结果：{path}")


def main():
    parser = argparse.ArgumentParser(description="事实支持校验评测")
    parser.add_argument("--mock", action="store_true", help="不调用判定模型，只检查用例")
    parser.add_argument("--qa", action="store_true", help="用内置语料对 12 个真实问题做端到端抽查")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    if args.qa:
        asyncio.run(run_qa())
        return
    report = asyncio.run(run(args.mock, args.concurrency))
    if report["unsupported_recall"] is not None and report["unsupported_recall"] < TARGET_RECALL:
        print(f"召回率低于 {TARGET_RECALL:.0%} 的目标。")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
