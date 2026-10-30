# 部署到香港服务器

单台服务器 + Docker Compose：Caddy（HTTPS + 访问口令）→ app 容器（FastAPI + 前端 + MCP 子进程）→ 千问 API；Postgres 容器存会话和长期记忆。

> 演示只用虚拟数据，不录入真实患者信息（服务器在境外，真实健康数据会涉及数据出境）。

## 1. 需要你自己准备的

| 项目 | 建议 |
|---|---|
| 服务器 | 阿里云轻量应用服务器或腾讯云轻量（Lighthouse），**地域选中国香港**，2 核 2G 起，系统选 Ubuntu 22.04/24.04 |
| 域名 | 任意注册商买一个便宜域名；香港服务器**不需要 ICP 备案** |
| DNS | 加一条 A 记录：`demo.你的域名` → 服务器公网 IP |
| 防火墙 | 控制台里只放行 22、80、443 |

## 2. 服务器初始化（SSH 登录后执行一次）

```bash
curl -fsSL https://get.docker.com | sh
```

```bash
git clone <你的仓库地址> health-agent && cd health-agent
```

（仓库是私有的话，用 deploy key，或者在本地 `rsync -av --exclude .venv --exclude .env ./ 服务器:~/health-agent/` 传上去。）

## 3. 配置 .env（只放服务器上，不进 git）

先生成访问口令的哈希：

```bash
docker run --rm caddy:2 caddy hash-password --plaintext '给客户的口令'
```

然后写 `.env`：

```
QWEN_API_KEY=...            # 这四行照抄本地 .env
QWEN_BASE_URL=...
QWEN_MODEL=...
QWEN_EMBEDDING_MODEL=...
DOMAIN=demo.你的域名.com
PG_PASSWORD=换成一个随机长密码
DEMO_USER=client
DEMO_PASSWORD_HASH='$2a$14$......'
```

口令哈希里有 `$`，**必须用单引号包起来**，否则 docker compose 会把它当变量替换掉。

## 4. 启动

```bash
docker compose up -d --build
```

首次启动 Caddy 会自动申请 HTTPS 证书（需要 DNS 已生效、80/443 已放行）。查看日志：

```bash
docker compose logs -f
```

## 5. 更新

```bash
git pull && docker compose up -d --build
```

## 6. 演示前检查清单

- [ ] 用手机 4G 打开链接，输入口令，完整走一遍示例对话
- [ ] 确认模型额度够用：账号开了"只用免费额度"时，额度用完会返回 403，回复会退回模板，轨迹里显示"模型暂不可用"。正式演示前关掉这个模式并充值
- [ ] 在阿里云百炼控制台给这个 API key 设消费上限
- [ ] 演示前 10 分钟先跑一轮预热
- [ ] 笔记本上留一份本地版本作后备：`uv run uvicorn app.main:app --port 8000`

## 注意

- 回复是 SSE 流式推送。Caddy 默认会立即转发 `text/event-stream`；不要在 Caddyfile 里加 `encode gzip`，压缩会把流缓冲住。如果改用 Nginx，必须设置 `proxy_buffering off`。
- 会话和长期记忆存在 Postgres（compose 自动配置 `DATABASE_URL`），容器重启不会丢。记得定期备份 `pgdata` 数据卷。
