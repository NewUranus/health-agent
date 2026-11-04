#!/usr/bin/env bash
# 部署固定版本镜像；就绪失败时恢复先前的应用与 Worker 镜像。
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p ops/.state
chmod 700 ops/.state
compose=(docker compose -f docker-compose.yml -f ops/compose.runtime.yml)
action=${1:-}
release_image=${2:-}
if [[ "$action" == "rollback" ]]; then
  release_image=$(cat ops/.state/previous-image)
elif [[ "$action" != "deploy" || -z "$release_image" ]]; then
  echo '用法：bash ops/release.sh deploy ghcr.io/组织/镜像:sha-提交号；或 bash ops/release.sh rollback' >&2
  exit 2
fi
if [[ ! "$release_image" =~ ^ghcr\.io/[a-z0-9._/-]+(:sha-[a-f0-9]{40}|@sha256:[a-f0-9]{64})$ ]]; then
  echo '只接受固定提交号或摘要的 GHCR 镜像。' >&2
  exit 2
fi
export HEALTHPILOT_IMAGE="$release_image"
previous_id=$("${compose[@]}" ps -q app)
previous_image=""
if [[ -n "$previous_id" ]]; then
  previous_image=$(docker inspect --format '{{.Config.Image}}' "$previous_id")
fi
"${compose[@]}" pull app worker
"${compose[@]}" up -d --no-build app worker
ready=false
for ((attempt=0; attempt<30; attempt++)); do
  if "${compose[@]}" exec -T app python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/api/v1/ready", timeout=5)' >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 3
done
if [[ "$ready" == true ]]; then
  if [[ -n "$previous_image" && "$previous_image" != "$release_image" ]]; then
    printf '%s\n' "$previous_image" > ops/.state/previous-image
  fi
  printf '%s\n' "$release_image" > ops/.state/current-image
  echo "应用与 Worker 已就绪：$release_image"
else
  if [[ -n "$previous_image" ]]; then
    export HEALTHPILOT_IMAGE="$previous_image"
    "${compose[@]}" up -d --no-build app worker
    echo '新版本未就绪，已请求恢复之前的应用与 Worker 镜像；请核对运行状态。' >&2
  else
    echo '首个版本未就绪，请检查 Worker、数据库和配置。' >&2
  fi
  exit 1
fi
