# 部署说明（GCP / Debian 12）

## 架构

```
本地 Windows                          GCP 服务器（private-proxy-us）
  └─ git push ──────────────► /srv/apps/gemini-fat-fish
                                   ├─ gemini-fish.service      server.py  :8766
                                   ├─ gemini-fish-qq.service   onebot_bridge.py :8767
                                   └─ gemini-fish-update.timer 每 5 分钟拉一次 git
```

服务器直连 Gemini API，不需要代理。网页端只监听 `127.0.0.1:8766`，要从本地访问可以走 SSH 隧道：

```bash
ssh -L 8766:127.0.0.1:8766 gcp
# 然后本地打开 http://127.0.0.1:8766
```

## 服务器目录

```
/srv/apps/gemini-fat-fish/
├── .env              密钥与配置（600，不进 git）
├── memory.json       网页端记忆（不进 git）
├── sessions/         各群 / 私聊记忆（不进 git）
├── logs/             server.log / bridge.log / qq-*.log
└── data/             qq_budget.json
```

## 首次安装（已执行）

```bash
sudo mkdir -p /srv/apps && sudo chown "$(id -un):$(id -gn)" /srv/apps
cd /srv/apps && git clone https://github.com/xiansakana/gemini-fat-fish.git
# 上传 .env 后
chmod 600 /srv/apps/gemini-fat-fish/.env
sudo cp deploy/gemini-fish.service deploy/gemini-fish-qq.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gemini-fish.service gemini-fish-qq.service
```

## 自动更新

`gemini-fish-update.timer` 每 5 分钟执行 `deploy/update.sh`：

1. `git fetch origin main`
2. 本地 HEAD 与 origin/main 不同才 `git pull --ff-only`
3. 有更新就 `systemctl restart gemini-fish gemini-fish-qq`

安装：

```bash
sudo cp deploy/gemini-fish-update.service deploy/gemini-fish-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gemini-fish-update.timer
```

手动触发一次：

```bash
sudo systemctl start gemini-fish-update.service
journalctl -u gemini-fish-update.service -n 20 --no-pager
```

`.env`、`memory.json`、`sessions/` 都在 `.gitignore` 里，`git pull` 不会覆盖。

> 自动更新已于 2026-11-27 在 `private-proxy-us` 上验证：push 后手动触发 timer，服务器拉到新提交并重启了两个服务。

## 常用命令

```bash
sudo systemctl status gemini-fish gemini-fish-qq
sudo systemctl restart gemini-fish gemini-fish-qq
tail -f /srv/apps/gemini-fat-fish/logs/server.log
tail -f /srv/apps/gemini-fat-fish/logs/bridge.log
curl -s http://127.0.0.1:8766/api/state | head -c 300
curl -s http://127.0.0.1:8767/health
```

## NapCat 的情况

服务器上没有部署 NapCat，原因：

- NapCat 上游仓库已删除，官方 Docker 镜像 `mlikiowa/napcat-docker` 也已下架
- 目前只有第三方镜像（如 `lmq8267/napcat`，3.3 万次拉取、1 star），供应链风险不明
- NapCat 需要扫码登录 QQ，服务器无图形界面，扫码体验差

三个可选方案：

| 方案 | 做法 | 代价 |
| --- | --- | --- |
| A. 本地跑 NapCat + SSH 隧道 | NapCat 在本地 Windows，`ssh -R`/`-L` 打通 3000 和 8767 | 本地需保持开机和隧道 |
| B. 服务器用第三方镜像 | `docker run lmq8267/napcat` | 镜像不可审计，QQ 小号风险 |
| C. 只跑网页端 | 服务器只做陪伴本体，QQ 以后再说 | 没有群聊 |

当前服务器上的桥是干跑模式（`QQ_ONEBOT_API` 为空），不影响网页端。
