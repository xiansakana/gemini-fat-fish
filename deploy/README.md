# 部署说明（GCP / Debian 12）

## 架构

```
本地 Windows                          GCP 服务器（private-proxy-us）
  └─ git push ──────────────► /srv/apps/gemini-fat-fish
                                   ├─ gemini-fish.service      server.py  :8766
                                   ├─ gemini-fish-qq.service   onebot_bridge.py :8767
                                   ├─ gemini-fish-logview.service deploy/logview.py :8770
                                   └─ gemini-fish-update.timer 每 5 分钟拉一次 git
```

## 后台日志页

`deploy/logview.py` 是一个只读日志页，覆盖群消息、桥 stdout/stderr、核心 journal、NapCat 容器日志、自动更新日志，外加一个「运行状态」标签看服务、容器和桥的 health。

```bash
sudo cp deploy/gemini-fish-logview.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gemini-fish-logview
```

### 443 端口复用（sslh）

GCP 防火墙只放行 `21/22/443`，而 21/22 会被浏览器拦，所以日志页走 443，用 sslh 按协议分流：

```
                       ┌─ TLS（VLESS Reality）→ 127.0.0.1:8443  sing-box
公网 443 → sslh ───────┤
                       └─ 明文 HTTP          → 127.0.0.1:8770  logview
```

- `/etc/default/sslh`：`DAEMON_OPTS="-p 0.0.0.0:443 --tls 127.0.0.1:8443 --http 127.0.0.1:8770 -t 5 -P /run/sslh.pid -u sslh"`
- sing-box 的 `private-reality` 入站改成 `listen: 127.0.0.1`、`listen_port: 8443`（原配置备份在 `/etc/sing-box/config.json.bak-*`）
- 验证方式：`curl http://<IP>:443/healthz` 应返回 200；`openssl s_client -connect <IP>:443 -servername www.microsoft.com` 应返回真实的 `www.microsoft.com` 证书，说明代理链路没被破坏
- 回滚：恢复 sing-box 配置 → `systemctl restart sing-box` → `systemctl disable --now sslh`

注意：日志页只能走 **http://**，写成 https:// 会被 sing-box 接走。

`.env` 里必须设置 `LOGVIEW_TOKEN`（`openssl rand -hex 12`），否则服务拒绝启动。访问地址：

```
http://<服务器IP>:8770/?token=<LOGVIEW_TOKEN>
```

带 `?token=` 打开一次会写入 HttpOnly Cookie，之后直接访问 `http://<服务器IP>:8770/` 即可。页面不提供任何写操作，也不读 `.env`、会话和记忆文件；换 token 后旧链接立即失效。

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
└── data/             qq_budget.json / qq_observe_budget.json / qq_bot_messages.json
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

## 入站上报（重要）

NapCat 在 Docker 里，容器访问宿主机的 `host.docker.internal` 会解析到 docker 网关 `172.17.0.1`。早期桥只监听 `127.0.0.1:8767`，NapCat 日志报 `connect ECONNREFUSED 172.17.0.1:8767`，群里真实消息**根本没进桥**——只有 `/simulate` 手动灌的消息有反应。

现在的做法：

- 服务器 `.env` 设 `QQ_BRIDGE_HOST=0.0.0.0`，桥监听所有地址，`127.0.0.1` 仍可用。
- 用 iptables 只放行本机和 docker 网段，其余来源直接丢：

```bash
sudo iptables -I INPUT -p tcp --dport 8767 -s 127.0.0.0/8 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 8767 -s 172.16.0.0/12 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 8767 -j DROP
```

验证：`docker exec napcat curl http://host.docker.internal:8767/health` 应返回 JSON，群里真实 @ 后日志应出现该条消息。

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

