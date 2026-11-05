#!/usr/bin/env bash
# 在服务器以部署用户执行；公钥文件必须作为参数传入，私钥不上传服务器。
set -euo pipefail
deploy_source=$(cd -- "$(dirname -- "$0")" && pwd)
deploy_key=${1:?请提供部署公钥文件}
[[ $(id -u) == 0 ]] || { echo '当前服务器部署入口需要 root 安装。' >&2; exit 1; }
[[ -f "$deploy_key" && $(wc -l < "$deploy_key") -eq 1 ]]
read -r deploy_key_type deploy_key_body _ < "$deploy_key"
[[ "$deploy_key_type" == ssh-ed25519 && "$deploy_key_body" =~ ^[A-Za-z0-9+/=]+$ ]]
ssh-keygen -l -f "$deploy_key" >/dev/null
install -d -m 755 /usr/local/libexec
install -m 755 "$deploy_source/ci-deploy.sh" /usr/local/libexec/healthpilot-ci-deploy
install -d -m 700 /root/.ssh
touch /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
deploy_entry="restrict,command=\"/usr/local/libexec/healthpilot-ci-deploy\" $deploy_key_type $deploy_key_body healthpilot-github-actions"
if ! grep -Fqx -- "$deploy_entry" /root/.ssh/authorized_keys; then
  if grep -Fq -- "$deploy_key_body" /root/.ssh/authorized_keys; then
    echo '此公钥已有其他授权配置，停止安装，请先核对。' >&2
    exit 1
  fi
  printf '\n%s\n' "$deploy_entry" >> /root/.ssh/authorized_keys
fi
echo '专用公钥已安装；只允许检查与固定提交部署，禁止交互终端及端口转发。'
