#!/usr/bin/env bash
# 安装到仓库外，作为部署专用 SSH 密钥的强制命令；不执行客户端传来的 shell。
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
deploy_dir=${HEALTHPILOT_DEPLOY_DIR:-/root/health-agent}
deploy_repository=ghcr.io/newuranus/health-agent
deploy_command=${SSH_ORIGINAL_COMMAND:-}
if [[ "$deploy_command" == check ]]; then
  deploy_sha=""
elif [[ "$deploy_command" =~ ^deploy\ ([a-f0-9]{40})$ ]]; then
  deploy_sha=${BASH_REMATCH[1]}
else
  echo '只允许 check 或 deploy 加完整提交号。' >&2
  exit 2
fi

cd -- "$deploy_dir"
[[ "$(git rev-parse --show-toplevel)" == "$(pwd -P)" ]]
[[ "$(git branch --show-current)" == main ]] || { echo '服务器必须位于 main 分支。' >&2; exit 1; }
[[ -f .env && -f docker-compose.yml ]]
command -v flock >/dev/null
docker compose version
if [[ "$deploy_command" == check ]]; then
  echo '专用 SSH 密钥、部署目录与 Docker 检查通过；未执行部署。'
  exit 0
fi

# 锁文件位于 Git 工作区外，避免影响工作区检查或被版本更新替换。
exec 9>"$(git rev-parse --absolute-git-dir)/healthpilot-deploy.lock"
flock -w 600 9 || { echo '等待其他部署完成超时。' >&2; exit 1; }
deploy_changes=$(git status --porcelain)
if [[ -n "$deploy_changes" && "$deploy_changes" != ' M Caddyfile' ]]; then
  echo '服务器有未提交修改，停止部署，请先核对。' >&2
  git status --short >&2
  exit 1
fi
export GIT_TERMINAL_PROMPT=0
git fetch origin main
git merge-base --is-ancestor "$deploy_sha" FETCH_HEAD || { echo '目标提交不属于远端 main。' >&2; exit 1; }
deploy_head=$(git rev-parse HEAD)
if [[ "$deploy_sha" != "$deploy_head" ]] && git merge-base --is-ancestor "$deploy_sha" HEAD; then
  echo '服务器已经包含更新提交，跳过过期部署。'
  exit 0
fi
git merge-base --is-ancestor HEAD "$deploy_sha" || { echo '服务器历史与目标提交分叉，停止部署。' >&2; exit 1; }
if [[ "$deploy_changes" == ' M Caddyfile' ]] && ! git diff --quiet HEAD "$deploy_sha" -- Caddyfile; then
  echo '目标提交修改了服务器自定义的 Caddyfile，停止部署。' >&2
  exit 1
fi
git cat-file -e "$deploy_sha:ops/release.sh"
git cat-file -e "$deploy_sha:ops/compose.runtime.yml"
deploy_image="$deploy_repository:sha-$deploy_sha"
# 先确认镜像可下载，再移动服务器代码；不读取分支最新 SHA 作为镜像版本。
docker pull "$deploy_image"
git merge --ff-only "$deploy_sha"
bash ops/release.sh deploy "$deploy_image"
printf '部署完成：%s\n' "$deploy_sha"
