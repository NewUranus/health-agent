"""检索身份由服务端设置；默认仅可读共享资料，None 仅用于管理员路径。"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json

from app.rag import Chunk

principal = ContextVar("knowledge_principal", default="")


@contextmanager
def knowledge_scope(user_id):
    token = principal.set(user_id)
    try:
        yield
    finally:
        principal.reset(token)


def allowed(document):
    user = principal.get()
    return not document.get("withdrawn", False) and (user is None or document.get("visibility", "shared") == "shared"
        or (user.startswith("account:") and user[8:] in document.get("allowed_user_ids", [])))


def sql_access():
    user = principal.get()
    return "AND NOT d.withdrawn AND (%s OR d.visibility = 'shared' OR d.allowed_user_ids ? %s)", (user is None, user[8:] if user and user.startswith("account:") else "")


@dataclass
class KnowledgeChunk(Chunk):
    document_id: int | None = None
    version: str = ""
    revision: int = 1


def evidence_key(chunk):
    return hashlib.sha256(json.dumps([chunk.get("id"), chunk.get("text"), chunk.get("version", ""), chunk.get("revision", 1)], ensure_ascii=False).encode()).hexdigest()


def evidence_refs(chunks):
    """记录本次读取过的全部依据，不依赖模型是否主动标注引用。"""
    return [{"id": chunk["id"], "fingerprint": evidence_key(chunk)} for chunk in chunks]


def references_valid(refs, current):
    return all(ref["id"].startswith("web-") or
        (ref["id"] in current and ref["fingerprint"] == evidence_key(current[ref["id"]])) for ref in refs)


def merge_evidence(*groups):
    return list({(ref['id'], ref['fingerprint']): ref for group in groups for ref in group}.values())
