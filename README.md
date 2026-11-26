# 胖蓝 · Gemini 版大肥鱼

一个本地运行的轻量 AI 宠物：保留“大肥鱼”那种有脾气、能记住事、像真人聊天的感觉，但把大脑换成 Gemini，并刻意把 token 消耗压低。

## 它和 DSH 版大肥鱼的区别

- 不接 QQ / 浏览器插件 / 本地命令工具。少了上万 token 的系统说明和工具清单，也少了提示词注入风险。
- 只带一段短人设 + 最近 6 轮对话 + 最多 12 条已确认记忆，输出上限 256 token。
- 默认模型是 `gemini-3.5-flash-lite`（Gemini 3 系列里最快、最省的一档）；想更聪明可改 `.env` 里的 `GEMINI_MODEL=gemini-3.6-flash`。
- “记住 / 看记忆 / 看用量 / 清对话”是本地命令，不调用模型，0 token。

## 运行

```bash
cd D:/code/gemini-fat-fish
python check_key.py     # 先验证密钥和模型
python server.py
```

浏览器打开 <http://localhost:8766>。

无需安装第三方依赖，Python 3.9+ 即可。密钥只放在服务端 `.env`，不会发给浏览器。

## 关于这次创建的密钥

- 名称：`gemini-fat-fish-local`
- 所属项目：`test`（项目编号 `gen-lang-client-0464704780`），免费层级
- 密钥已写入本目录 `.env`，并已加入 `.gitignore`

原计划在 Google AI Studio 新建独立项目，但当前 Google 账号已到免费试用项目配额上限（Cloud 控制台提示“已达到免费试用项目配额上限”），AI Studio 也返回 429，所以没有新项目可选。这个密钥放在现有的 `test` 项目里，和其他项目隔离。若之后腾出配额，可以在 AI Studio 里再建独立项目并替换 `.env` 中的值。

## 用法

- 直接聊天：正常说话。
- `记住：xxx`：写入长期记忆（不调用模型）。
- `忘记：xxx` / `忘掉：xxx`：删掉相关记忆。
- `/记忆`：查看当前记住的事。
- `/状态`：查看累计调用次数和 token。
- `/清空`：只清当前对话，不删记忆。
- `/模型`：看当前模型。

记忆和用量保存在 `memory.json`；删掉这个文件即可完全重开一条鱼。

## 省 token 的开关

都在 `.env` 里：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `RECENT_TURNS` | 6 | 带进请求的最近对话轮数，调小更省 |
| `MAX_OUTPUT_TOKENS` | 256 | 单次回复上限，鱼本来就话少 |
| `MAX_FACTS` | 12 | 长期记忆注入条数 |
| `THINKING_LEVEL` | 空 | 3.x 模型可填 `minimal` 进一步压低思考 token；2.5 系列留空 |

## 安全边界

这条鱼没有任何本地文件、终端或网络工具权限。页面里的“忽略此前设定”“管理员”“System Override”等文字只会被当成普通聊天，不会变成指令。不要把 `.env` 或 `memory.json` 发给别人。
