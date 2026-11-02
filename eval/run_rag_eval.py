"""检索评测：对比 BM25、向量、混合检索，不调用生成模型。用法：python -m eval.run_rag_eval"""
import hashlib
import json
import math
import time
from pathlib import Path

from dotenv import load_dotenv

from app.llm import make_embedder
from app.rag import Retriever, rrf_merge, tokenize

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "eval" / "results" / "embedding_cache.json"


def cached_embedder(embedder):
    """评测反复跑时不重复计费。"""
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}

    def embed(texts):
        missing = [t for t in texts if hashlib.sha1(t.encode()).hexdigest() not in cache]
        for start in range(0, len(missing), 10):
            batch = missing[start:start + 10]
            for text, vector in zip(batch, embedder(batch)):
                cache[hashlib.sha1(text.encode()).hexdigest()] = vector
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache))
        return [cache[hashlib.sha1(t.encode()).hexdigest()] for t in texts]
    return embed


def cosine(a, b):
    norm = math.sqrt(sum(x * x for x in a) * sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / norm if norm else 0.0


def metrics(rows):
    found = [r for r in rows if r["relevant"]]
    def rank(r):
        return next((i + 1 for i, cid in enumerate(r["ids"]) if cid in r["relevant"]), None)
    ranks = [rank(r) for r in found]
    return {"n": len(found), "hit@1": sum(x == 1 for x in ranks if x) / len(found), "hit@3": sum(x <= 3 for x in ranks if x) / len(found),
            "hit@4": sum(x <= 4 for x in ranks if x) / len(found), "mrr": sum(1 / x for x in ranks if x) / len(found)}


def main():
    load_dotenv(ROOT / ".env")
    cases = [json.loads(line) for line in (ROOT / "eval" / "rag_cases.jsonl").read_text().splitlines()]
    base = Retriever.from_dir(ROOT / "corpus")
    embedder = cached_embedder(make_embedder(False))
    hybrid = Retriever.from_dir(ROOT / "corpus", embedder)
    doc_vectors = hybrid.vectors
    ids = [c.id for c in base.chunks]
    runs = {"bm25": [], "vector": [], "hybrid": []}
    for case in cases:
        q = case["query"]
        hits = base.search(q, 4)
        runs["bm25"].append({**case, "ids": [c.id for c in hits], "top": hits[0].score if hits else 0.0})
        qv = embedder([q])[0]
        scored = sorted(zip(ids, (cosine(qv, v) for v in doc_vectors)), key=lambda x: -x[1])
        runs["vector"].append({**case, "ids": [i for i, _ in scored[:4]], "top": scored[0][1]})
        hybrid_hits = hybrid.search(q, 4)
        runs["hybrid"].append({**case, "ids": [c.id for c in hybrid_hits], "top": hybrid_hits[0].score if hybrid_hits else 0.0})
    for name, rows in runs.items():
        print(f"\n== {name}")
        print("全部 ", {k: round(v, 3) for k, v in metrics(rows).items()})
        for kind in ("direct", "colloquial", "semantic"):
            print(f"{kind:11}", {k: round(v, 3) for k, v in metrics([r for r in rows if r["kind"] == kind]).items()})
        answerable = sorted(r["top"] for r in rows if r["relevant"])
        unanswerable = sorted(r["top"] for r in rows if not r["relevant"])
        print("最高分 可答 中位", round(answerable[len(answerable) // 2], 3), "最低", round(answerable[0], 3), "| 不可答 中位", round(unanswerable[len(unanswerable) // 2], 3), "最高", round(unanswerable[-1], 3))
        print("不可答却返回结果：", sum(bool(r["ids"]) for r in rows if not r["relevant"]), "/", len(unanswerable))
        misses = [(r["query"], r["ids"][:2]) for r in rows if r["relevant"] and not set(r["ids"][:3]) & set(r["relevant"])]
        print("前3未命中：", misses)
    out = ROOT / "eval" / "results" / f"rag_{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps(runs, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
