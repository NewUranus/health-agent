"""本地 BM25 与可选向量检索，使用 RRF 融合。"""
import logging
import math
import re
from dataclasses import dataclass, replace
from pathlib import Path

import jieba
from rank_bm25 import BM25Okapi
from app.terms import expand_query

logger = logging.getLogger(__name__)
jieba.setLogLevel(logging.WARNING)


@dataclass
class Chunk:
    id: str
    title: str
    source: str
    text: str
    score: float = 0.0


def tokenize(text):
    text = text.lower()
    return [word for word in jieba.lcut_for_search(text) if re.search(r"\w", word)] + re.findall(r"[\u4e00-\u9fff]", text)


def _rrf_scores(rankings, k):
    scores = {}
    for ranking in rankings:
        for rank, cid in enumerate(dict.fromkeys(ranking), 1):
            scores[cid] = scores.get(cid, 0) + 1 / (k + rank)
    return scores


def rrf_merge(rankings: list[list[str]], k: int = 60) -> list[str]:
    scores = _rrf_scores(rankings, k)
    return sorted(scores, key=scores.get, reverse=True)


class Retriever:
    def __init__(self, chunks, embedder=None):
        self.chunks = chunks
        self.index = BM25Okapi([tokenize(c.title + " " + c.text) for c in chunks]) if chunks else None
        self.embedder = embedder
        self.vectors = None
        if embedder:
            try:
                self.vectors = embedder([c.title + " " + c.text for c in chunks])
                if len(self.vectors) != len(chunks):
                    raise ValueError("向量数量与语料不一致")
            except Exception:
                logger.warning("向量索引构建失败，退回 BM25")
                self.embedder = None

    @classmethod
    def from_dir(cls, path, embedder=None):
        chunks = []
        for file in sorted(Path(path).glob("*.md")):
            raw = file.read_text(encoding="utf-8")
            _, header, body = raw.split("---", 2)
            metadata = dict(line.split(":", 1) for line in header.strip().splitlines())
            chunks.append(Chunk(file.stem, metadata["title"].strip(), metadata["source"].strip(), body.strip()))
        return cls(chunks, embedder)

    def search(self, query, k=4):
        if not self.index or k <= 0:
            return []
        query = expand_query(query)
        scores = self.index.get_scores(tokenize(query))
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        hits = [replace(self.chunks[i], score=float(scores[i])) for i in ranked if scores[i] > 0]
        if self.embedder and self.vectors is not None:
            try:
                vector = self.embedder([query])[0]
                def cosine(i):
                    other = self.vectors[i]
                    if len(other) != len(vector):
                        raise ValueError("向量维度不一致")
                    norm = math.sqrt(sum(x*x for x in vector) * sum(x*x for x in other))
                    return sum(x*y for x, y in zip(vector, other)) / norm if norm else 0
                ranked_vectors = sorted(range(len(self.chunks)), key=cosine, reverse=True)
                rankings = [[c.id for c in hits], [self.chunks[i].id for i in ranked_vectors if cosine(i) > 0]]
                fused = _rrf_scores(rankings, 60)
                lookup = {c.id: c for c in self.chunks}
                return [replace(lookup[cid], score=fused[cid]) for cid in rrf_merge(rankings)[:k]]
            except Exception:
                logger.warning("向量查询失败，本次退回 BM25")
        return hits[:k]
