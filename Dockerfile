FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

WORKDIR /app
# 不预编译字节码：jieba 的大词典文件在小内存机器上会编译超时
ENV UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# 先只装依赖，代码改动时可以复用这一层缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

# 演示规模 1 个 worker 足够，也避免每个 worker 各起一个 MCP 子进程
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
