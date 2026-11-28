# 踩坑记录

这里记录开发 / 部署 / 调试过程中真实踩过的坑和犯过的错，按时间倒序排列。
目的：同样的坑不踩第二次。每次出问题并解决后，**必须**在这里追加一条。

格式：`日期 · 一句话标题` → 现象 / 根因 / 修复 / 教训。涉及服务器操作的，写清楚完整命令或参数，不要只写「改了配置」。

---

## 2026-11-28 · 新增函数用了 `glob` 但没导入，运行时 NameError

**现象**：按群话题刷 B 站功能上线后，调用 `recent_group_topics()` 直接报 `NameError: name 'glob' is not defined`。

**根因**：`server.py` 原来没导入 `glob`，新函数里用了 `glob.glob(...)`。`py_compile` 只检查语法，不检查名字是否存在。

**修复**：`import glob` 加进 `server.py` 顶部。

**教训**：
1. 新写函数用了哪些模块级函数/常量，落地前先在脑子里过一遍导入。`py_compile` 通过 ≠ 能跑。
2. 改完必须**真调用一次新函数**（我这次是部署后才用 `python3 -c` 调 `recent_group_topics()` 才发现）。
3. 同类问题已出现三次（force_search 作用域、query_mode 作用域、glob 未导入），统一教训：**新增代码路径必须有至少一次真实执行验证**，不能只看编译结果。

---

## 2026-11-28 · 查询模式 `query_mode` 作用域错误，/api/chat 直接 NameError

**现象**：加了查询模式后，核心服务每次请求都崩，桥报 `RemoteDisconnected`；err 日志 `NameError: name 'query_mode' is not defined`。

**根因**：`query_mode` 定义在 `call_gemini()` 函数内部，但我把它写进了 `do_POST` 的 JSON 返回里——两个不同的作用域，`do_POST` 里根本没有这个变量。（之前踩过一次同类的坑：`force_search` 定义在 contents 组装之后，也是作用域/顺序问题。）

**修复**：在 `do_POST` 里用同样的判断重算一遍：`query_mode = channel == "group" and search_requested(message)`。

**教训**：
1. 跨函数传递状态要么放进返回值，要么在使用的地方重新算，别假设函数内的局部变量在外面可见。
2. `py_compile` 过不了这类错误——它是运行时 NameError。**改完必须真实调用一次** `/api/chat` 看返回，不能只编译。
3. 同一类错误（变量作用域/定义顺序）已经踩了两次（force_search、query_mode），以后新增「模式标志」变量时先在脑子里过一遍：它在哪定义、在哪用、中间隔了几个函数。

---

## 2026-11-28 · 重建 NapCat 容器漏掉 `host.docker.internal` 映射，群里 @ 全部没反应

**现象**：三个 systemd 服务都 active，NapCat `get_status` 返回 `online: true`，容器 up，但群里 @ 她没有任何反应，群消息日志停在几小时前。

**根因**：NapCat 的消息上报地址是 `http://host.docker.internal:8767/onebot`。昨天为了加自动登录环境变量重建容器时，只保留了端口 / 挂载 / 网络 / 环境变量，**漏了原容器的 `--add-host host.docker.internal:host-gateway`**。容器日志刷屏 `getaddrinfo ENOTFOUND host.docker.internal`——QQ 侧消息收到了，但上报全部 DNS 解析失败，桥一条都拿不到。

**修复**：重建容器时补回该参数。以后重建 NapCat **必须**完整保留下面这组参数：

```bash
docker run -d --name napcat --restart always \
  --network napcat_default \
  --add-host host.docker.internal:host-gateway \
  -v /srv/apps/napcat/ntqq:/app/.config/QQ:rw \
  -v /srv/apps/napcat/config:/app/napcat/config:rw \
  -p 127.0.0.1:3000:3000 -p 127.0.0.1:3001:3001 -p 127.0.0.1:6099:6099 \
  -e ACCOUNT=3343472272 \
  -e NAPCAT_QUICK_ACCOUNT=3343472272 \
  -e NAPCAT_QUICK_PASSWORD=<QQ密码> \
  -e NAPCAT_UID=0 -e NAPCAT_GID=0 -e TZ=Asia/Shanghai \
  mlikiowa/napcat-docker:latest
```

**教训**：
1. 「账号在线」不等于「消息链路通」。验证必须走完整链路：模拟一条群消息 POST 到 `8767/onebot`，确认 `logs/qq-YYYY-MM-DD.log` 里出现 `[入站]`。只看 `get_status` 会漏掉这种上报断链。
2. 重建容器前先 `docker inspect <container> > /tmp/xxx-inspect.json` 备份完整配置，重建时逐项对照，别凭记忆列参数。
3. 查掉线 / 没反应问题，先看 `docker logs napcat`，错误 often 直白写在那里。

---

## 2026-11-28 · 模型年份乱猜（说今天是 2024 年）的真正根因

**现象**：她查资料时自拟的搜索词带错误年份（「2024」「3月3日」），答「Switch 2 还没官宣」这类过期信息；直接问「今天几号」也能答错。

**根因**：`time_context()` 返回给 system prompt 的时间只有「上午 11:46」，**没有日期和年份**。模型只能凭训练记忆猜。之前只在「强制搜索指令」里注入日期是治标不治本——普通对话里她照样不知道今天几号。

**修复**：`time_context()` 返回改成 `2026年11月28日 上午 11:46` 这种完整格式，全局生效。

**教训**：上下文里「她应该知道」的东西，必须显式注入。不要假设模型知道当前日期 —— 它没有实时时钟，全靠 prompt 里给。

---

## 2026-11-28 · Bing RSS 中文搜索质量差，但优先级还排在 DDG 前面

**现象**：查中文资料（尤其消费电子、财经）搜回百度百科「switch 是英语单词」这类垃圾，她基于垃圾结果答错。

**根因**：`search_web` 的实现是「Bing RSS 优先、DDG 兜底」，而实测同一查询 DDG 的中文结果质量明显更好（能搜到 2026 年真实资讯）。好结果永远排在烂结果后面，只有 Bing 完全失败时才轮到 DDG。

**修复**：优先级调换为 DDG 优先、Bing 兜底；DDG 从 lite 版换成 html 版，解析摘要（`result__snippet`）并解开 `uddg=` 跳转。

**教训**：
1. 兜底顺序要按实测质量排，不是按「谁先写的」排。
2. 不同出口 IP 的搜索结果差异巨大：本地测 DDG 结果很差，GCP 服务器上正常。**以实际部署环境为准**，别在本地下结论。
3. GCP 服务器出网宽松（Google / DDG / SearXNG 都通），和某些白名单出网环境不同，别照搬别人的「DDG 被封」结论。

---

## 2026-11-28 · `force_search` 定义在 contents 之后导致 UnboundLocalError

**现象**：核心服务起不来，桥报 `RemoteDisconnected`；err 日志里 `UnboundLocalError: cannot access local variable 'force_search'`。

**根因**：改强制搜索逻辑时，把 `force_search = group and search_requested(message)` 写在了 `contents` 组装之后，但组装过程中就要用它。函数内局部变量在赋值前被引用 → 崩。

**修复**：把 `force_search` 的定义提到函数开头（在计算 `max_tokens` 之前）。

**教训**：改完代码先 `python -m py_compile server.py`，再在服务器上跑一次真实 `/api/chat`。编译能过不代表运行不崩 —— 局部变量作用域错误只有运行时才暴露。

---

## 2026-11-28 · Gemini 原生 `google_search` 免费层 429

**现象**：给 `generateContent` 加 `tools: [{"google_search": {}}]` 后，两个模型都返回 HTTP 429 RESOURCE_EXHAUSTED；不带工具则正常。

**根因**：这个 Google AI 项目是免费层，文字模型的 Search grounding 未开放（只有付费层 / 部分 Live 模型免费层支持）。不是代码问题。

**结论**：要么绑卡开付费层（每月 5000 次免费搜索），要么继续用自建的 DDG/专用源。**在免费层别指望原生 grounding**。

**教训**：`AI Studio 网页里能搜` ≠ `你的 API key 能搜`。网页界面走它自己的额度和开关，验证必须以代码直调 API 为准。

---

## 2026-11-27 · 历史回填翻页停在第一页

**现象**：进新群补历史，翻来翻去总是同一批消息。

**根因**：用 `min(message_seq)` 选最旧消息，但 NapCat 的 `message_seq` 是和时间无关的 ID，不是按时间递增的。翻页游标选错，永远定位不到更早的消息。

**修复**：改成按 `time` 字段选最旧的一条：`seq = int(min(timed, key=lambda item: float(item["time"]))["message_seq"])`。

**教训**：OneBot 的 `message_seq` 不等于时间顺序。分页必须按 `time` 定位。

---

## 2026-11-27 · 回填永不重跑

**现象**：进群时补过一次历史，之后再也不补，群里新消息一直没进上下文。

**根因**：判断逻辑是 `group_id not in _backfilled`，但 `_backfilled` 从文件恢复时已经包含了该群，于是永远判定「补过了」。

**修复**：改成时间戳判断 `now - float(_backfilled.get(group_id, 0.0)) > 6 * 3600`，超过 6 小时自动重跑。

**教训**：「是否做过」的状态判断要用时间戳 + 窗口，不要用集合成员判断——持久化恢复后集合状态会和预期不符。

---

## 2026-11-27 · 群聊查行情 503 直接抛给用户，没有熔断

**现象**：群里查 A 股报 `HTTP 503`，直接显示「调用失败」。

**根因**：备用模型固定为 `gemini-3.5-flash-lite`，而群聊主模型也是 lite。主备相同 → 代码只打一次，503 无回退；且没有失败计数，过载时反复撞同一个挂掉的模型。

**修复**：主备相同时自动取另一档；按模型记录失败次数，10 分钟窗口内失败 3 次就垫底，成功一次清零自愈（参考 youtube-digest）。

**教训**：主备模型配置相同时要自动纠错，别假设用户配的一定对。熔断窗口对 24/7 常驻服务要用分钟级，不能用批任务的「按天禁用」。

---

## 2026-11-27 · 评估 JSON 格式错误：模型在字符串里塞未转义引号

**现象**：观察式发言评估报 `Expecting ',' delimiter`，整条消息丢弃。

**根因**：模型输出的 JSON 字符串值里有未转义的英文引号 / 换行 / 尾逗号。

**修复**：`parse_json_object` + `_repair_json_text` 做状态机修复；Gemini 侧设置 `responseMimeType=application/json`。

**教训**：让模型输出 JSON 必须有容错解析器。别指望模型永远输出合法 JSON。

---

## 通用教训（跨坑）

- **调试、灌消息、真实调用验证一律在 GCP 服务器上做**，不在本地起服务。本地出口 IP、环境、配置和服务器都可能不同，本地验证通过的结论在服务器上不一定成立。
- **验证要看完整链路**：账号在线 ≠ 消息上报通；工具调用记录要看 `result` 字段（被 ONCE_TOOLS 拦下的调用也会记一条 event）；API 返回要看 `tool_events` 才知道她到底搜没搜。
- **改完必验证**：`py_compile` → 部署 → 真实 `/api/chat` 或模拟上报 → 看日志。
- **build/重建类操作先备份原配置**，逐项对照，别凭记忆。
