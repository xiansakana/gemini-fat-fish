#!/usr/bin/env python3
"""OneBot v11 HTTP 桥：让 Gemini 娘住进 QQ 群 / 私聊。

只依赖 Python 标准库。核心对话仍由 server.py 负责，本文件只做：
接收 OneBot 事件 → 判断该不该接话 → 调 /api/chat → 拆条发回 QQ。

用法：
  1. 先启动 server.py（默认 127.0.0.1:8766）
  2. 在 .env 里配置 QQ_* 项（见 .env.example）
  3. python onebot_bridge.py
  4. OneBot 实现（NapCat / LLOneBot 等）的 HTTP 上报地址填：
     http://127.0.0.1:8767/onebot

安全提醒：非官方 QQ 协议有封号风险，务必使用小号。
"""

from __future__ import annotations

import json
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import CFG, ROOT  # noqa: E402

LOG_DIR = ROOT / "logs"
DATA_DIR = ROOT / "data"
BUDGET_FILE = DATA_DIR / "qq_budget.json"
BOT_MESSAGES_FILE = DATA_DIR / "qq_bot_messages.json"

CORE_TIMEOUT = 90
_reply_lock = threading.Lock()
_group_last_reply: dict[str, float] = {}
_bot_message_ids: list[str] = []


def cfg(key: str, default: str = "") -> str:
    return str(CFG.get(key) or default).strip()


def parse_list(key: str) -> list[str]:
    raw = cfg(key)
    return [item.strip() for item in re.split(r"[,，\s]+", raw) if item.strip()]


def group_whitelist() -> set[str]:
    return set(parse_list("QQ_GROUP_WHITELIST"))


def private_whitelist() -> set[str]:
    return set(parse_list("QQ_PRIVATE_WHITELIST"))


def wake_words() -> list[str]:
    return parse_list("QQ_WAKE_WORDS") or ["小双", "Gemini娘", "双子"]


def interject_probability() -> float:
    try:
        return max(0.0, min(1.0, float(cfg("QQ_INTERJECT", "0.05"))))
    except ValueError:
        return 0.05


def cooldown_seconds() -> int:
    try:
        return max(0, int(cfg("QQ_COOLDOWN", "25")))
    except ValueError:
        return 25


def daily_calls() -> int:
    try:
        return max(0, int(cfg("QQ_DAILY_CALLS", "300")))
    except ValueError:
        return 300


def core_url() -> str:
    return cfg("QQ_CORE", "http://127.0.0.1:8766").rstrip("/")


def onebot_api() -> str:
    """留空即干跑模式：只在控制台打印，不真发 QQ。"""
    return cfg("QQ_ONEBOT_API").rstrip("/")


def log_line(text: str) -> None:
    text = str(text).replace("\r", " ").replace("\n", " / ")
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"qq-{time.strftime('%Y-%m-%d')}.log"
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(time.strftime("%H:%M:%S ") + text + "\n")
    except OSError:
        pass


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def budget_ok() -> bool:
    today = time.strftime("%Y-%m-%d")
    data = load_json(BUDGET_FILE, {})
    if data.get("date") != today:
        data = {"date": today, "count": 0}
    if int(data.get("count", 0)) >= daily_calls():
        return False
    data["count"] = int(data.get("count", 0)) + 1
    save_json(BUDGET_FILE, data)
    return True


def remember_bot_message(message_id) -> None:
    if message_id is None:
        return
    _bot_message_ids.append(str(message_id))
    del _bot_message_ids[:-100]
    save_json(BOT_MESSAGES_FILE, _bot_message_ids)


def is_reply_to_bot(reply_id) -> bool:
    if reply_id is None:
        return False
    if not _bot_message_ids:
        _bot_message_ids.extend(str(x) for x in load_json(BOT_MESSAGES_FILE, []))
    return str(reply_id) in _bot_message_ids


def parse_message(message) -> tuple[str, bool, str | None, int]:
    """返回（文本，是否@机器人，引用的消息 id，图片数量）。"""
    if isinstance(message, str):
        at_self = "[CQ:at,qq=" in message and cfg("QQ_BOT_QQ") and f"[CQ:at,qq={cfg('QQ_BOT_QQ')}]" in message
        reply_match = re.search(r"\[CQ:reply,id=(-?\d+)\]", message)
        images = len(re.findall(r"\[CQ:image,", message))
        text = re.sub(r"\[CQ:[^\]]+\]", " ", message)
        return re.sub(r"\s+", " ", text).strip(), at_self, reply_match.group(1) if reply_match else None, images

    text_parts: list[str] = []
    at_self = False
    reply_id = None
    images = 0
    bot_qq = cfg("QQ_BOT_QQ")
    for segment in message or []:
        kind = segment.get("type")
        data = segment.get("data") or {}
        if kind == "text":
            text_parts.append(str(data.get("text", "")))
        elif kind == "at":
            qq = str(data.get("qq", ""))
            if qq == "all":
                text_parts.append("@全体成员 ")
            elif bot_qq and qq == bot_qq:
                at_self = True
            else:
                text_parts.append(f"@{qq} ")
        elif kind == "reply":
            reply_id = str(data.get("id")) if data.get("id") is not None else None
        elif kind in ("image", "mface"):
            images += 1
            text_parts.append("[图片] ")
        elif kind == "face":
            text_parts.append("[表情] ")
    text = re.sub(r"\s+", " ", "".join(text_parts)).strip()
    return text, at_self, reply_id, images


def display_name(event: dict) -> str:
    sender = event.get("sender") or {}
    name = sender.get("card") or sender.get("nickname") or str(event.get("user_id", ""))
    return str(name)[:20]


def should_engage(event: dict, text: str, at_self: bool, reply_to_bot: bool) -> tuple[bool, str]:
    if event.get("message_type") == "private":
        if str(event.get("user_id")) in private_whitelist():
            return True, "私聊白名单"
        return False, "私聊不在白名单"

    group_id = str(event.get("group_id"))
    if group_id not in group_whitelist():
        return False, "群不在白名单"

    if at_self:
        return True, "被@"
    if reply_to_bot:
        return True, "回复了她"
    for word in wake_words():
        if word and word in text:
            return True, "叫了名字"
    if text and random.random() < interject_probability():
        return True, "随机插话"
    return False, "没被叫"


def cooling(group_id: str, now: float) -> bool:
    last = _group_last_reply.get(group_id, 0.0)
    return now - last < cooldown_seconds()


def clean_for_group(text: str) -> str:
    """群聊里去掉括号旁白，并丢掉去掉后为空的段落。"""
    text = re.sub(r"[（(][^）)]{0,40}[）)]", "", text or "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def split_reply(text: str, limit: int = 2) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    parts = [part.strip() for part in re.split(r"\n+", text) if part.strip()]
    if len(parts) == 1 and len(parts[0]) > 90:
        sentences = re.split(r"(?<=[。！？!?])", parts[0])
        merged, current = [], ""
        for sentence in sentences:
            if not sentence.strip():
                continue
            if len(current) + len(sentence) > 90 and current:
                merged.append(current.strip())
                current = sentence
            else:
                current += sentence
        if current.strip():
            merged.append(current.strip())
        parts = merged
    return [part[:400] for part in parts[:limit]]


def ask_core(text: str, session: str, channel: str, speaker: str) -> tuple[str, str | None]:
    payload = json.dumps(
        {"message": text, "session": session, "channel": channel, "speaker": speaker},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        core_url() + "/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=CORE_TIMEOUT) as response:
            data = json.load(response)
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}: {exc}"
    return str(data.get("reply") or ""), data.get("error")


def call_onebot(action: str, payload: dict) -> bool:
    api = onebot_api()
    if not api:
        print(f"[干跑] {action} {json.dumps(payload, ensure_ascii=False)[:300]}", flush=True)
        return True
    request = urllib.request.Request(
        f"{api}/{action}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.load(response)
        if isinstance(result, dict) and result.get("retcode") not in (0, None):
            log_line(f"发送失败 {action}: {result}")
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        log_line(f"发送异常 {action}: {type(exc).__name__} {exc}")
        return False


def build_segments(parts: list[str], group: bool, reply_id: str | None, user_id) -> list[list[dict]]:
    messages: list[list[dict]] = []
    for index, part in enumerate(parts):
        segments: list[dict] = []
        if index == 0 and group and reply_id:
            segments.append({"type": "reply", "data": {"id": str(reply_id)}})
        if index == 0 and group and user_id:
            segments.append({"type": "at", "data": {"qq": str(user_id)}})
        segments.append({"type": "text", "data": {"text": part}})
        messages.append(segments)
    return messages


def handle_event(event: dict, simulated: bool = False) -> dict:
    """返回处理结果，便于模拟器直接看到判定过程。"""
    if event.get("post_type") not in (None, "message"):
        return {"ok": False, "reason": "非消息事件"}

    message_type = event.get("message_type")
    if message_type not in ("group", "private"):
        return {"ok": False, "reason": "未知消息类型"}

    text, at_self, reply_id, images = parse_message(event.get("message"))
    if images:
        text = (text + " [对方发了图片]").strip()
    if not text:
        return {"ok": False, "reason": "空消息"}

    reply_to_bot = is_reply_to_bot(reply_id)
    engage, reason = should_engage(event, text, at_self, reply_to_bot)
    if not engage:
        return {"ok": False, "reason": reason}

    group = message_type == "group"
    group_id = str(event.get("group_id", ""))
    user_id = event.get("user_id")
    session = f"group_{group_id}" if group else f"private_{user_id}"
    channel = "group" if group else "private"
    speaker = display_name(event)

    now = time.time()
    if group and cooling(group_id, now):
        return {"ok": False, "reason": "冷却中"}
    if group and event.get("message_id") is not None:
        reply_id = str(event.get("message_id"))

    if not budget_ok():
        log_line(f"预算用完，跳过 {session}")
        return {"ok": False, "reason": "今日预算用完"}

    prompt = f"{speaker}：{text}" if group else text
    with _reply_lock:
        reply, error = ask_core(prompt, session, channel, speaker)

    if error or not reply:
        log_line(f"{session} | {speaker} | {text} | 调用失败: {error}")
        return {"ok": False, "reason": f"核心调用失败: {error}"}

    parts = split_reply(clean_for_group(reply) if group else reply)
    messages = build_segments(parts, group, reply_id, user_id)

    for index, segments in enumerate(messages):
        if index:
            time.sleep(0.8)
        if group:
            payload = {"group_id": int(group_id), "message": segments}
            ok = call_onebot("send_group_msg", payload)
            if not ok and any(seg.get("type") == "at" for seg in segments):
                # @ 的号可能不是群成员，NapCat 拿不到 Uid，去掉 @ 重发
                fallback = [seg for seg in segments if seg.get("type") != "at"]
                ok = call_onebot("send_group_msg", {"group_id": int(group_id), "message": fallback})
        else:
            ok = call_onebot(
                "send_private_msg",
                {"user_id": int(user_id), "message": segments},
            )
        if not ok:
            break

    if group:
        _group_last_reply[group_id] = time.time()
    log_line(f"{session} | {speaker} | {text} | {reply[:200]}")
    return {
        "ok": True,
        "reason": reason,
        "session": session,
        "reply": reply,
        "parts": parts,
        "dry_run": not bool(onebot_api()),
        "simulated": simulated,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "GeminiQQ/1.0"

    def log_message(self, fmt: str, *args) -> None:  # 静默 OneBot 的心跳噪声
        pass

    def _json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return {}

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(
                {
                    "ok": True,
                    "core": core_url(),
                    "onebot": onebot_api() or "dry-run",
                    "groups": sorted(group_whitelist()),
                    "private": sorted(private_whitelist()),
                    "interject": interject_probability(),
                    "cooldown": cooldown_seconds(),
                }
            )
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/onebot":
            event = self._body()
            # OneBot 要求快速返回，处理放后台线程
            threading.Thread(target=handle_event, args=(event,), daemon=True).start()
            self.send_response(204)
            self.end_headers()
            return
        if self.path == "/simulate":
            body = self._body()
            event = {
                "post_type": "message",
                "message_type": body.get("message_type", "group"),
                "group_id": body.get("group_id", "10000"),
                "user_id": body.get("user_id", "20000"),
                "message_id": body.get("message_id", str(int(time.time()))),
                "sender": {"nickname": body.get("nickname", "测试群友")},
                "message": [{"type": "text", "data": {"text": body.get("text", "")}}],
            }
            self._json(handle_event(event, simulated=True))
            return
        self._json({"error": "not found"}, 404)


def main() -> None:
    port = int(cfg("QQ_BRIDGE_PORT", "8767"))
    print(f"QQ 桥已启动：http://127.0.0.1:{port}", flush=True)
    print(f"核心：{core_url()}", flush=True)
    print(f"OneBot：{onebot_api() or '未配置（干跑模式，只打印不发送）'}", flush=True)
    print(f"群白名单：{sorted(group_whitelist()) or '空（不会回任何群）'}", flush=True)
    print(f"私聊白名单：{sorted(private_whitelist()) or '空（不会回任何私聊）'}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
