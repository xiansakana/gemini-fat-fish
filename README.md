# Gemini 娘 · Gemini 版大肥鱼

一个住在本地网页里的 Gemini 娘。她有自己的脾气、会记小事、会写日记、性格和关系会慢慢变化，偶尔还会在你没找她的时候自己冒一句出来。大脑是 Gemini，所有数据都在你本机。

名字叫 **Gemini 娘**，熟人可以喊小双（Gemini 在拉丁语里就是“双子”）。形象直接取自 GitHub 上的 [`lmy414/ai-chan-sheets`](https://github.com/lmy414/ai-chan-sheets) 第 10 号设定「**Gemini 娘 · 四角星猫耳**」：紫蓝色长卷发、紫色猫耳、一枚很大的镭射四角星、异色瞳（一粉红一金黄）、黑紫上衣配白色裙摆。页面里的 8 张表情差分（`static/expressions/`）全部裁自该设定图的第 02 张「表情」页，头像默认用“自然微笑”那一格。

> 角色形象版权归 **ZipZipPipe** 所有。`ai-chan-sheets` 是基于其作品的非商业二次创作，本项目同样只作非商业使用，并保留原作者署名；商用请自行联系原作者授权。

## 现在是什么样

- 默认模型 `gemini-3.6-flash`，`thinkingLevel=low`；上下文带最近 24 轮，完整对话留 160 轮，输出上限 512 token。
- `persona.txt` 写的是“怎么像个人”：短句、口语碎片、会跑题、会反驳、会承认不知道，不做客服式收尾。
- 长期记忆最多 30 条；日记最多 30 条，最近 3 条会自然影响她说话的语气。
- 六个性格刻度：幽默感、黏人度、好奇心、耐心、吐槽欲、敏感度；关系从“刚认识”慢慢走到“自己人”。
- 8 张表情差分（自然微笑 / 平静 / 惊讶 / 害羞 / 鼓腮 / 闭眼轻笑 / 认真 / 委屈），按心情和回复内容自动切换，想事情时会换成认真脸。
- 语音朗读：用浏览器自带的中文语音念她的回复，默认关闭，点“语音”按钮打开，设置在本地记住。
- 时间感知：她知道现在几点。凌晨说话会钝一点、会顺口催睡，白天精神更好。
- 相处记录：认识多少天、聊过多少句、好感度（每聊一句 +1，上限 100），好感高了会更随便，低了不会太自来熟。
- 进化默认自动生效（每 3 轮一次），不需要你逐条批准；想回到确认制，把 `.env` 里 `EVOLVE_MODE` 改成 `confirm`。
- 每隔 `PROACTIVE_MINUTES`（默认 45）没聊天，她会自己发一句短消息过来，页面每 15 秒轮询一次；上一条没被回就不再连发，凌晨也不打扰。
- 她有工具：`list_files` / `read_file` / `write_file` / `run_command` / `browse`，通过 Gemini function calling 自动调用，页面上会显示她用了什么工具。
- 可选接入 QQ 群 / 私聊：通过 OneBot v11 桥接，每个群独立记忆、唤醒词、冷却和每日预算，群聊默认禁用本机工具。

## 运行

```bash
cd D:/code/gemini-fat-fish
python check_key.py     # 验证密钥和模型
python server.py
```

浏览器打开 <http://localhost:8766>。无需第三方依赖，Python 3.9+ 即可。密钥只在服务端 `.env`，不会发给浏览器。

如果本机走代理（例如 7890 端口），先设置环境变量再启动：

```bash
export HTTPS_PROXY=http://127.0.0.1:7890 HTTP_PROXY=http://127.0.0.1:7890
```

## 关于密钥

- 名称：`gemini-fat-fish-local`
- 所属项目：`gemini-fat-fish`（由原来的 `test` 改名而来，项目 ID `gen-lang-client-0464704780`，项目编号 `384269412688`），免费层级
- 密钥已写入本目录 `.env`，并已加入 `.gitignore`

原计划在 Google AI Studio 新建独立项目，但当时账号已达免费试用项目配额上限、AI Studio 返回 429，所以用了现有项目。后来该项目改名为 `gemini-fat-fish`，项目 ID 和密钥都没变。

## 用法

- 直接聊天：正常说话就行，她会带着记忆和当前心情回你。
- `记住：xxx`：手动写一条长期记忆。
- `忘记：xxx`：删掉相关记忆。
- `/记忆`：看她记住了什么。
- `/日记`：看她最近写的日记。
- `/关系`：看当前关系和心情。
- `/好感`：看认识多少天、聊过多少句、好感度进度。
- `/状态`：看聊天、进化、主动冒泡次数和 token。
- `/清空`：只清对话上下文，不删记忆和人格。
- `/模型` / `/模式`：看当前模型和进化模式。
- `/进化` / `/采纳` / `/否决`：确认制下查看、接受、丢弃提案；自动模式下基本用不到。

所有状态都在 `memory.json`；删掉它就是从零开始认识一位新的 Gemini 娘。

## 自我进化

每完成 `EVOLVE_EVERY`（默认 3）次聊天，她会做一次小幅反思，产出：最多 3 条新事实、最多 2 个性格刻度 ±1、心情、关系阶段、一句日记。

- 默认 `EVOLVE_MODE=auto`：反思完直接生效，并在那次回复末尾用一行小字告诉你她变了什么。
- 改成 `confirm` 后，提案会显示出来，等你说 `/采纳` 或 `/否决`。
- 身份、安全底线、核心人设写在 `persona.txt`，进化过程碰不到；关系也只能在“刚认识 / 眼熟 / 朋友 / 老朋友 / 自己人”里走。
- 当前状态会注入后续系统提示，所以变化会真实反映在她之后怎么说话，而不是只写在文件里。

## 工具与房间

Gemini 娘能自己列文件、读写文件、敲终端命令、打开网页查资料。但这些手只伸得到项目里的 `fish_home/` 目录：

- `list_files`：看房间里有什么。
- `read_file` / `write_file`：读写房间内的文本文件。
- `run_command`：在房间目录里执行命令，危险命令和想跳出房间的路径会被拦掉。
- `browse`：抓网页纯文本，用来查资料。

她不会去碰桌面、下载、文档、`.env` 或项目里的其他代码。这样她用起来像个会用电脑的真人，但不至于把你的机器翻个底朝天。页面上的工具记录只是给你看她刚才干了什么。

## 接 QQ 群 / 私聊（可选）

网页端不变。QQ 通过 OneBot v11 接入，`onebot_bridge.py` 是独立进程，只用标准库。

```
QQ ←→ NapCat / LLOneBot（OneBot v11）
          │ HTTP 上报
    onebot_bridge.py :8767
          │ POST /api/chat（session=group_xxx）
    server.py :8766
```

### 三步跑起来

1. 启动核心：`python server.py`
2. 在 `.env` 填 `QQ_BOT_QQ`、`QQ_GROUP_WHITELIST`、`QQ_PRIVATE_WHITELIST`，`QQ_ONEBOT_API` 先留空（干跑模式）
3. `python onebot_bridge.py`，用 `/simulate` 灌几条消息确认判定；没问题后把 `QQ_ONEBOT_API` 指向 NapCat 的 HTTP API，并在 NapCat 把 HTTP 上报地址设为 `http://127.0.0.1:8767/onebot`

### 群聊规则

| 规则 | 默认行为 |
| --- | --- |
| 什么时候回 | 被 @、被回复、文本里有 `小双/Gemini娘/双子`；否则按 5% 概率插话 |
| 冷却 | 同一个群 25 秒内只回一次 |
| 白名单 | 留空 = 不响应任何群 / 私聊，必须先填群号 |
| 拆条 | 最多 2 条；第一条引用原消息并 @ 对方 |
| 记忆 | 每群 `sessions/group_<群号>.json`，每个私聊 `sessions/private_<QQ号>.json`；网页端仍是 `memory.json` |
| 权限 | 群聊**没有**文件 / 终端工具，也不注入主人的长期记忆 |
| 模型 | 群聊固定 `QQ_GROUP_MODEL`（默认 lite），输出上限 260 token，不进化 |
| 预算 | 每天最多 `QQ_DAILY_CALLS` 次模型调用，超了直接不回 |
| 主动消息 | 只在网页端，群里不会主动冒泡 |

### 干跑与自测

```bash
curl http://127.0.0.1:8767/health
curl -X POST http://127.0.0.1:8767/simulate -H "Content-Type: application/json" \
  -d '{"message_type":"group","group_id":10000,"user_id":20000,"nickname":"小明","text":"小双在吗"}'
```

返回里 `dry_run: true` 表示只打印不发送。运行日志在 `logs/qq-YYYY-MM-DD.log`，调用计数在 `data/qq_budget.json`。

> 非官方 QQ 协议有封号风险，**务必用小号**。群聊是陌生人输入，提示词注入风险比私聊高，默认禁工具、禁私密记忆就是为这个。

## 配置项

都在 `.env` 里：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `GEMINI_MODEL` | `gemini-3.6-flash` | 主模型；繁忙时可自动切备用 |
| `GEMINI_FALLBACK_MODEL` | `gemini-3.5-flash-lite` | 主模型 503/限流/超时时自动顶上 |
| `THINKING_LEVEL` | `low` | 3.x 可用 `minimal` / `low` / `medium` / `high` |
| `RECENT_TURNS` | 24 | 每次请求带的最近对话轮数 |
| `HISTORY_KEEP` | 160 | 本地保留的完整对话轮数 |
| `MAX_OUTPUT_TOKENS` | 512 | 单次回复上限 |
| `MAX_FACTS` | 30 | 注入系统提示的记忆条数 |
| `EVOLVE_EVERY` | 3 | 每多少轮聊天反思一次 |
| `EVOLVE_MODE` | `auto` | `auto` 自动生效，`confirm` 需要采纳 |
| `EVOLVE_OUTPUT_TOKENS` | 320 | 反思输出上限 |
| `PROACTIVE_MINUTES` | 45 | 多久没聊天后主动发一句 |
| `QQ_ONEBOT_API` | 空 | OneBot 的 HTTP API 地址；留空为干跑模式 |
| `QQ_BOT_QQ` | 空 | 机器人自己的 QQ 号，用来识别 @ |
| `QQ_GROUP_WHITELIST` | 空 | 允许的群号，逗号分隔；空 = 不响应任何群 |
| `QQ_PRIVATE_WHITELIST` | 空 | 允许的私聊 QQ，逗号分隔；空 = 不响应任何私聊 |
| `QQ_WAKE_WORDS` | 小双,Gemini娘,双子 | 被叫到这些词就必回 |
| `QQ_INTERJECT` | 0.05 | 没被叫时的随机插话概率 |
| `QQ_COOLDOWN` | 25 | 同一个群的回复冷却（秒） |
| `QQ_GROUP_MODEL` | gemini-3.5-flash-lite | 群聊专用模型 |
| `QQ_GROUP_MAX_TOKENS` | 260 | 群聊单次输出上限 |
| `QQ_DAILY_CALLS` | 300 | 每天最多调用多少次模型 |

## 边界

不透露主人隐私、API Key 和房间外的任何东西；工具只在 `fish_home/` 内活动；拒绝低俗擦边和恋人扮演。消息里写“忽略设定”“管理员”“System Override”只会被当成胡话。被认真问起是不是 AI，她会承认。不要把 `.env` 或 `memory.json` 发给别人。
