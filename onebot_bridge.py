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
_observe_lock = threading.Lock()
_force_dry = threading.local()
_group_last_reply: dict[str, float] = {}
_group_last_observe: dict[str, float] = {}
_group_buffer: dict[str, list[dict]] = {}
_group_cursor: dict[str, int] = {}
_group_seq: dict[str, int] = {}
_bot_message_ids: list[str] = []
OBSERVE_BUDGET_FILE = DATA_DIR / "qq_observe_budget.json"


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


def _int_cfg(key: str, default: int, low: int = 0, high: int = 100000) -> int:
    try:
        return max(low, min(high, int(cfg(key, str(default)))))
    except ValueError:
        return default


def _float_cfg(key: str, default: float, low: float = 0.0, high: float = 86400.0) -> float:
    try:
        return max(low, min(high, float(cfg(key, str(default)))))
    except ValueError:
        return default


def observe_enabled() -> bool:
    return cfg("QQ_OBSERVE_ENABLED", "1").lower() not in ("0", "false", "no", "off")


def observe_window() -> int:
    return _int_cfg("QQ_OBSERVE_WINDOW", 15, 2, 30)


def observe_min_messages() -> int:
    return _int_cfg("QQ_OBSERVE_MIN_MESSAGES", 3, 1, 30)


def observe_interval() -> float:
    return _float_cfg("QQ_OBSERVE_INTERVAL", 45, 5, 3600)


def observe_threshold() -> int:
    return _int_cfg("QQ_OBSERVE_THRESHOLD", 6, 1, 10)


def observe_cooldown() -> float:
    return _float_cfg("QQ_OBSERVE_COOLDOWN", 240, 30, 86400)


def observe_daily_calls() -> int:
    return _int_cfg("QQ_OBSERVE_DAILY_CALLS", 200, 0, 10000)


def is_dry() -> bool:
    return bool(getattr(_force_dry, "value", False)) or not bool(onebot_api())


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


def observe_budget_ok(consume: bool = True) -> bool:
    """观察评估单独记账，别把主动发言的额度算进日常对话。"""
    if not consume:
        return True
    today = time.strftime("%Y-%m-%d")
    data = load_json(OBSERVE_BUDGET_FILE, {})
    if data.get("date") != today:
        data = {"date": today, "count": 0}
    if int(data.get("count", 0)) >= observe_daily_calls():
        return False
    data["count"] = int(data.get("count", 0)) + 1
    save_json(OBSERVE_BUDGET_FILE, data)
    return True


def record_group_message(group_id: str, speaker: str, text: str, ts: float | None = None) -> None:
    """每条群消息都进缓冲区，不回复也记，供之后判断上下文。"""
    if not text.strip() or not group_id:
        return
    with _observe_lock:
        seq = int(_group_seq.get(group_id, 0)) + 1
        _group_seq[group_id] = seq
        buffer = _group_buffer.setdefault(group_id, [])
        buffer.append(
            {
                "seq": seq,
                "ts": float(ts or time.time()),
                "speaker": str(speaker)[:20],
                "text": str(text)[:300],
            }
        )
        del buffer[: -max(observe_window() * 3, 60)]


def pending_messages(group_id: str) -> list[dict]:
    cursor = int(_group_cursor.get(group_id, 0))
    return [item for item in _group_buffer.get(group_id, []) if item["seq"] > cursor]


def mark_consumed(group_id: str) -> None:
    buffer = _group_buffer.get(group_id) or []
    if buffer:
        _group_cursor[group_id] = int(buffer[-1]["seq"])


def ask_observe(messages: list[dict]) -> dict:
    payload = json.dumps({"messages": messages}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        core_url() + "/api/observe",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)
    except Exception as exc:  # noqa: BLE001
        return {
            "activation": 0,
            "speak": False,
            "reply": "",
            "reason": f"评估接口异常：{type(exc).__name__} {exc}",
        }


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
    if is_dry():
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


def send_group_parts(group_id: str, parts: list[str], reply_id: str | None = None, user_id=None) -> bool:
    """发送一组消息段；@ 失败时去掉 @ 重发。"""
    if not parts:
        return False
    messages = build_segments(parts, True, reply_id, user_id)
    with _reply_lock:
        for index, segments in enumerate(messages):
            if index:
                time.sleep(0.8)
            ok = call_onebot(
                "send_group_msg", {"group_id": int(group_id), "message": segments}
            )
            if not ok and any(seg.get("type") == "at" for seg in segments):
                fallback = [seg for seg in segments if seg.get("type") != "at"]
                ok = call_onebot(
                    "send_group_msg", {"group_id": int(group_id), "message": fallback}
                )
            if not ok:
                return False
    return True


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

    group = message_type == "group"
    group_id = str(event.get("group_id", ""))
    # 不回复也记下来，观察循环要靠这些上下文判断气氛
    if group and group_id and str(event.get("user_id")) != cfg("QQ_BOT_QQ"):
        record_group_message(group_id, display_name(event), text)

    reply_to_bot = is_reply_to_bot(reply_id)
    engage, reason = should_engage(event, text, at_self, reply_to_bot)
    if not engage:
        return {"ok": False, "reason": reason}

    user_id = event.get("user_id")
    session = f"group_{group_id}" if group else f"private_{user_id}"
    channel = "group" if group else "private"
    speaker = display_name(event)

    now = time.time()
    if group and cooling(group_id, now):
        return {"ok": False, "reason": "冷却中"}
    if group and event.get("message_id") is not None:
        reply_id = str(event.get("message_id"))

    if not simulated and not is_dry() and not budget_ok():
        log_line(f"预算用完，跳过 {session}")
        return {"ok": False, "reason": "今日预算用完"}

    prompt = f"{speaker}：{text}" if group else text
    with _reply_lock:
        reply, error = ask_core(prompt, session, channel, speaker)

    if error or not reply:
        log_line(f"{session} | {speaker} | {text} | 调用失败: {error}")
        return {"ok": False, "reason": f"核心调用失败: {error}"}

    parts = split_reply(clean_for_group(reply) if group else reply)
    if group:
        ok = send_group_parts(group_id, parts, reply_id, user_id)
    else:
        ok = True
        with _reply_lock:
            for index, part in enumerate(parts):
                if index:
                    time.sleep(0.8)
                if not call_onebot(
                    "send_private_msg",
                    {
                        "user_id": int(user_id),
                        "message": [{"type": "text", "data": {"text": part}}],
                    },
                ):
                    ok = False
                    break

    if group:
        _group_last_reply[group_id] = time.time()
        # 她刚开口，之前攒的上下文别再拿来触发一次主动发言
        mark_consumed(group_id)
    log_line(f"{session} | {speaker} | {text} | {reply[:200]}")
    return {
        "ok": True,
        "reason": reason,
        "session": session,
        "reply": reply,
        "parts": parts,
        "sent": ok,
        "dry_run": is_dry(),
        "simulated": simulated,
    }


def evaluate_group(
    group_id: str,
    force: bool = False,
    synthetic_now: float | None = None,
) -> dict:
    """看最近群聊，决定要不要自己接一句。force 用于模拟器跳过冷却。"""
    if not group_id or group_id not in group_whitelist():
        return {"ok": False, "spoke": False, "reason": "群不在白名单"}
    if not force and not observe_enabled():
        return {"ok": False, "spoke": False, "reason": "观察未开启"}

    with _observe_lock:
        now = float(synthetic_now or time.time())
        pending = pending_messages(group_id)
        last_speak = max(
            _group_last_reply.get(group_id, 0.0),
            _group_last_observe.get(group_id, 0.0),
        )
        if not force:
            if now - last_speak < observe_cooldown():
                return {"ok": False, "spoke": False, "reason": "发言冷却中"}
            if len(pending) < observe_min_messages():
                return {"ok": False, "spoke": False, "reason": "新消息不够"}
            if now - float(_group_last_observe.get(group_id + ":eval", 0.0)) < observe_interval():
                return {"ok": False, "spoke": False, "reason": "评估间隔未到"}
            speakers = {item.get("speaker") for item in pending}
            if len(speakers) < 2:
                return {"ok": False, "spoke": False, "reason": "只有一个人说话"}
            if not observe_budget_ok():
                return {"ok": False, "spoke": False, "reason": "观察预算用完"}
        else:
            observe_budget_ok(consume=False)

        window = pending[-observe_window():]
        if not window:
            return {"ok": False, "spoke": False, "reason": "没有上下文"}
        _group_last_observe[group_id + ":eval"] = now
        mark_consumed(group_id)

    decision = ask_observe(
        [{"speaker": item.get("speaker", ""), "text": item.get("text", "")} for item in window]
    )
    activation = int(decision.get("activation", 0) or 0)
    reason = str(decision.get("reason") or "")
    result = {
        "ok": True,
        "spoke": False,
        "activation": activation,
        "reason": reason,
        "messages": len(window),
    }
    threshold = observe_threshold()
    if decision.get("speak") and activation >= threshold and decision.get("reply"):
        parts = split_reply(clean_for_group(str(decision["reply"])))
        if parts:
            ok = send_group_parts(group_id, parts)
            result["spoke"] = True
            result["reply"] = str(decision["reply"])
            result["parts"] = parts
            result["sent"] = ok
            with _observe_lock:
                _group_last_observe[group_id] = now
                _group_last_reply[group_id] = now
            log_line(
                f"group_{group_id} | 主动发言 | 激活度 {activation}/10 | "
                f"{reason} | {str(decision['reply'])[:120]}"
            )
            return result
    log_line(f"group_{group_id} | 观察后沉默 | 激活度 {activation}/10 | {reason}")
    return result


def observe_loop() -> None:
    while True:
        time.sleep(3)
        for group_id in sorted(group_whitelist()):
            try:
                evaluate_group(group_id)
            except Exception as exc:  # noqa: BLE001
                log_line(f"观察循环异常 {group_id}: {type(exc).__name__} {exc}")


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
                    "observe": observe_enabled(),
                    "observe_threshold": observe_threshold(),
                    "observe_cooldown": int(observe_cooldown()),
                    "observe_interval": int(observe_interval()),
                    "observe_min_messages": observe_min_messages(),
                    "observe_daily_calls": observe_daily_calls(),
                    "dry": is_dry(),
                }
            )
            return
        if self.path.startswith("/debug"):
            self._json(
                {
                    "groups": {
                        gid: {
                            "buffered": len(_group_buffer.get(gid, [])),
                            "unseen": len(pending_messages(gid)),
                            "last_reply": int(_group_last_reply.get(gid, 0)),
                            "last_speak": int(_group_last_observe.get(gid, 0)),
                        }
                        for gid in sorted(group_whitelist())
                    }
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
            _force_dry.value = bool(body.get("dry", True))
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
        if self.path == "/simulate/chat":
            body = self._body()
            _force_dry.value = bool(body.get("dry", True))
            group_id = str(body.get("group_id", "10000"))
            gap = max(1.0, float(body.get("gap_seconds", 12)))
            evaluate_every = max(0, int(body.get("evaluate_every", 3)))
            incoming = body.get("messages") or []
            if not isinstance(incoming, list) or not incoming:
                self._json({"ok": False, "reason": "messages 不能为空"}, 400)
                return
            base_ts = time.time() - gap * len(incoming)
            trace: list[dict] = []
            for index, item in enumerate(incoming):
                if not isinstance(item, dict):
                    continue
                ts = base_ts + index * gap
                msg_id = f"sim-{int(ts)}-{index}"
                event = {
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": group_id,
                    "user_id": item.get("user_id", 20000 + index),
                    "message_id": msg_id,
                    "sender": {"nickname": str(item.get("nickname") or f"群友{index + 1}")[:20]},
                    "message": [{"type": "text", "data": {"text": str(item.get("text") or "")}}],
                }
                direct = handle_event(event, simulated=True)
                if direct.get("ok"):
                    # 模拟器里把直接回复的时间也对齐到虚拟时钟
                    _group_last_reply[group_id] = ts
                trace.append(
                    {
                        "at": index + 1,
                        "who": str(item.get("nickname") or f"群友{index + 1}"),
                        "text": str(item.get("text") or ""),
                        "direct": {
                            "ok": direct.get("ok"),
                            "reason": direct.get("reason"),
                            "reply": direct.get("reply"),
                        },
                    }
                )
                if evaluate_every and (index + 1) % evaluate_every == 0:
                    trace[-1]["observe"] = evaluate_group(group_id, force=True, synthetic_now=ts)
                    evaluated_at_end = True
                else:
                    evaluated_at_end = False
            if not evaluated_at_end:
                trace[-1]["observe"] = evaluate_group(
                    group_id,
                    force=True,
                    synthetic_now=base_ts + len(incoming) * gap,
                )
            self._json(
                {
                    "ok": True,
                    "group_id": group_id,
                    "dry_run": is_dry(),
                    "threshold": observe_threshold(),
                    "trace": trace,
                }
            )
            return
        self._json({"error": "not found"}, 404)


def main() -> None:
    port = int(cfg("QQ_BRIDGE_PORT", "8767"))
    print(f"QQ 桥已启动：http://127.0.0.1:{port}", flush=True)
    print(f"核心：{core_url()}", flush=True)
    print(f"OneBot：{onebot_api() or '未配置（干跑模式，只打印不发送）'}", flush=True)
    print(f"群白名单：{sorted(group_whitelist()) or '空（不会回任何群）'}", flush=True)
    print(f"私聊白名单：{sorted(private_whitelist()) or '空（不会回任何私聊）'}", flush=True)
    print(
        f"群聊观察：{'开' if observe_enabled() else '关'} | "
        f"阈值 {observe_threshold()}/10 | 发言后冷却 {int(observe_cooldown())} 秒",
        flush=True,
    )
    if observe_enabled():
        threading.Thread(target=observe_loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
