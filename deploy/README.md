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
└── data/             qq_budget.json / qq_observe_budget.json
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

## 群聊观察配置

桥每 3 秒检查白名单群：攒够 `QQ_OBSERVE_MIN_MESSAGES` 条新消息、距上次评估超过 `QQ_OBSERVE_INTERVAL` 秒、且至少两个人说过话，才调一次 `/api/observe`。激活度达到 `QQ_OBSERVE_THRESHOLD` 才发言，发言后 `QQ_OBSERVE_COOLDOWN` 秒内不再主动开口。评估不写会话记忆。

服务器 `.env` 里相关的正式值：

```
QQ_INTERJECT=0.02
QQ_OBSERVE_ENABLED=1
QQ_OBSERVE_MIN_MESSAGES=3
QQ_OBSERVE_INTERVAL=45
QQ_OBSERVE_THRESHOLD=6
QQ_OBSERVE_COOLDOWN=240
QQ_OBSERVE_DAILY_CALLS=200
```

本地灌一段群聊看判定（不会真发到 QQ）：

```bash
curl -s -X POST http://127.0.0.1:8767/simulate/chat \
  -H 'Content-Type: application/json' \
  -d '{"group_id":1129990537,"gap_seconds":12,"evaluate_every":4,"messages":[{"nickname":"小林","text":"刚才刷到个视频，猫摔下来还能稳稳落地"},{"nickname":"阿花","text":"猫真的有九条命"},{"nickname":"小林","text":"你说猫是不是知道自己很可爱"},{"nickname":"阿花","text":"肯定啊，每次闯祸都装无辜"}]}'
```

## NapCat（已部署）

官方 Docker 镜像 `mlikiowa/napcat-docker` 已随上游删库下架，镜像从另一台服务器 `navigation` 导出后传过来：

```bash
# 在 navigation 上
docker save mlikiowa/napcat-docker:latest | gzip -1 > /tmp/napcat-docker.tar.gz
# 传到 GCP（本地做管道，不落盘）
ssh navigation "cat /tmp/napcat-docker.tar.gz" | ssh gcp "cat > /tmp/napcat-docker.tar.gz"
# 在 GCP 上
gunzip -c /tmp/napcat-docker.tar.gz | docker load
```

容器配置在 `/srv/apps/napcat/docker-compose.yml`：

- bridge 网络，端口只映射到 `127.0.0.1`（3000 API / 3001 / 6099 WebUI），公网访问不到
- `shm_size: 256m`，挂载 `ntqq`（QQ 数据）和 `config`（OneBot 配置）
- `extra_hosts: host.docker.internal:host-gateway`

已登录小号 `3343472272`（昵称「大肥鱼」）。**重启容器一般不用重新扫码**，登录态在 `ntqq` 目录里。

### 配置要点

`config/onebot11_3343472272.json`：

- HTTP 服务 `host` 必须是 `0.0.0.0`。写 `127.0.0.1` 时 Docker 端口映射会 `Connection reset`，因为映射进来的是容器网卡地址。
- HTTP 上报地址必须用 `http://host.docker.internal:8767/onebot`，容器里的 `127.0.0.1` 是容器自己，不是宿主机。

改完配置 `docker restart napcat` 生效。

### 已知问题

- 给一个**不存在于群里的 QQ 号**发 `at`，NapCat 会报 `Get Uid Error`，整条消息发送失败。桥已做降级：去掉 `at` 重发。
- 首次用 host 网络启动时，entrypoint 的 Xvfb 还没就绪，QQ 会 `Missing X server or $DISPLAY` 然后 segfault。用 bridge 网络正常。

## 风险

- 非官方协议，**有封号风险，只能用小号**。
- 上游已删库，版本停在 v4.18.42，QQ 客户端升级后可能失效。
- 镜像来自第三方服务器，虽然 md5 校验过、容器隔离，但不等于可审计。

