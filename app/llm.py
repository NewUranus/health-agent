"""千问兼容接口和离线模板；日志不包含服务异常或密钥。"""
import logging
import os
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

logger = logging.getLogger(__name__)


class MockLLM:
    async def ainvoke(self, messages):
        return AIMessage(content="请补充健康信息；本演示不替代医生诊断。")


def make_llm(mock=False):
    if mock:
        return MockLLM()
    key = os.getenv("QWEN_API_KEY")
    if not key:
        return None
    return ChatOpenAI(api_key=key, base_url=os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
                      model=os.getenv("QWEN_MODEL", "qwen-plus"), temperature=0, timeout=30, max_retries=1,
                      # 千问默认开思考模式，同样的请求从约 2 秒变成 30 秒以上
                      extra_body={"enable_thinking": False})


def make_embedder(mock=False):
    if mock:
        return None
    if not os.getenv("QWEN_EMBEDDING_MODEL") or not os.getenv("QWEN_API_KEY"):
        logger.info("未配置向量模型或密钥，使用 BM25")
        return None
    embeddings = OpenAIEmbeddings(api_key=os.getenv("QWEN_API_KEY"), base_url=os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
                                  model=os.environ["QWEN_EMBEDDING_MODEL"], check_embedding_ctx_length=False, request_timeout=30, max_retries=0)
    return embeddings.embed_documents


def template_advice(chunks):
    if not chunks:
        return "暂无足够的指南依据，请向医生咨询具体问题。"
    return "\n".join(f"- **{c['title']}**：{c['text']} [cite:{c['id']}]" for c in chunks[:5])
