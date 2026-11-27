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

import base64
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
TALK_VALUE_FILE = DATA_DIR / "qq_talk_value.json"
PROFILE_FILE = DATA_DIR / "qq_profiles.json"
PERSONA_FILE = DATA_DIR / "qq_personas.json"
SLANG_FILE = DATA_DIR / "qq_slang.json"
INTRO_FILE = DATA_DIR / "qq_group_intro.json"
BACKFILL_FILE = DATA_DIR / "qq_backfill.json"

CORE_TIMEOUT = 90
_reply_lock = threading.Lock()
_observe_lock = threading.Lock()
_force_dry = threading.local()
_group_last_reply: dict[str, float] = {}
_group_last_observe: dict[str, float] = {}
_group_last_probe: dict[str, float] = {}
_group_last_bot_text: dict[str, str] = {}
_group_recent_replies: dict[str, list[str]] = {}
_group_talk_value: dict[str, float] = {}
_group_summary_counter: dict[str, int] = {}
_group_slang_counter: dict[str, int] = {}
_group_last_notice: dict[str, float] = {}
_group_buffer: dict[str, list[dict]] = {}
_group_cursor: dict[str, int] = {}
_group_seq: dict[str, int] = {}
_group_warmup: dict[str, float] = {}
_backfilled: dict[str, float] = {}
_bot_message_ids: list[str] = []
OBSERVE_BUDGET_FILE = DATA_DIR / "qq_observe_budget.json"


def cfg(key: str, default: str = "") -> str:
    return str(CFG.get(key) or default).strip()


def parse_list(key: str) -> list[str]:
    raw = cfg(key)
    return [item.strip() for item in re.split(r"[,，\s]+", raw) if item.strip()]


def group_whitelist() -> set[str]:
    return set(parse_list("QQ_GROUP_WHITELIST"))


def group_allowed(group_id: str) -> bool:
    """白名单填 * 表示任何群都生效（新拉的群不用改配置）。"""
    allowed = group_whitelist()
    return "*" in allowed or str(group_id) in allowed


def known_groups() -> set[str]:
    """白名单填 * 时没有固定群号，用实际见过的群代替。"""
    whitelist = {gid for gid in group_whitelist() if gid != "*"}
    if "*" not in group_whitelist():
        return whitelist
    return whitelist | set(_group_buffer) | set(load_json(INTRO_FILE, {}) or {})


def private_whitelist() -> set[str]:
    return set(parse_list("QQ_PRIVATE_WHITELIST"))


def admin_qq() -> set[str]:
    return set(parse_list("QQ_ADMIN_QQ"))


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


def direct_cooldown_seconds() -> float:
    """被 @ / 叫名字 / 被回复时的最短间隔，比普通插话短得多。"""
    return _float_cfg("QQ_DIRECT_COOLDOWN", 5, 0, 60)


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


def sticker_rate() -> float:
    return _float_cfg("QQ_STICKER_RATE", 0.08, 0.0, 1.0)


def notice_rate() -> float:
    return _float_cfg("QQ_NOTICE_RATE", 0.5, 0.0, 1.0)


def notice_gap() -> float:
    return _float_cfg("QQ_NOTICE_GAP", 60, 10, 3600)


def context_probe_enabled() -> bool:
    return cfg("QQ_CONTEXT_PROBE", "1").lower() not in ("0", "false", "no", "off")


def context_probe_window() -> float:
    return _float_cfg("QQ_CONTEXT_PROBE_WINDOW", 180, 30, 3600)


def context_probe_gap() -> float:
    return _float_cfg("QQ_CONTEXT_PROBE_GAP", 12, 3, 300)


def observe_daily_calls() -> int:
    return _int_cfg("QQ_OBSERVE_DAILY_CALLS", 200, 0, 10000)


def backfill_count() -> int:
    """进群或第一次见到某个群时，最多往回翻多少条历史消息。"""
    return _int_cfg("QQ_BACKFILL_COUNT", 200, 0, 1000)


def buffer_max() -> int:
    """每群保留多少条消息当上下文，太大会占内存，但回填要靠它留住。"""
    return _int_cfg("QQ_BUFFER_MAX", 240, 60, 2000)


def group_warmup() -> float:
    """刚进群这段时间只潜水，不主动插话（秒）。"""
    return _float_cfg("QQ_GROUP_WARMUP", 300, 0, 86400)


def auto_accept_invite() -> bool:
    """管理员邀请入群时自动同意，不然她根本进不去。"""
    return cfg("QQ_AUTO_ACCEPT_INVITE", "1").lower() not in ("0", "false", "no", "off")


def join_greet_enabled() -> bool:
    """被拉进一个还没人说话的新群时，打个招呼。"""
    return cfg("QQ_JOIN_GREET", "1").lower() not in ("0", "false", "no", "off")


def is_dry() -> bool:
    return bool(getattr(_force_dry, "value", False)) or not bool(onebot_api())


def bridge_host() -> str:
    """默认只听本机；容器里的 NapCat 上报需要改成 0.0.0.0 或 docker 网关地址。"""
    return cfg("QQ_BRIDGE_HOST", "127.0.0.1").strip() or "127.0.0.1"


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


def record_group_message(
    group_id: str, speaker: str, text: str, ts: float | None = None, msg_id: str = ""
) -> None:
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
                "msg_id": str(msg_id or ""),
            }
        )
        del buffer[: -max(observe_window() * 3, 60, buffer_max())]


def pending_messages(group_id: str) -> list[dict]:
    cursor = int(_group_cursor.get(group_id, 0))
    return [item for item in _group_buffer.get(group_id, []) if item["seq"] > cursor]


def mark_consumed(group_id: str) -> None:
    buffer = _group_buffer.get(group_id) or []
    if buffer:
        _group_cursor[group_id] = int(buffer[-1]["seq"])


def _norm_text(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(text or "")).lower()


def _bigrams(text: str) -> set[str]:
    value = _norm_text(text)
    if len(value) < 2:
        return {value} if value else set()
    return {value[i:i + 2] for i in range(len(value) - 1)}


def _similar(a: str, b: str) -> bool:
    left, right = _bigrams(a), _bigrams(b)
    if not left or not right:
        return False
    union = left | right
    return len(left & right) / len(union) >= 0.62


def is_duplicate(group_id: str, text: str) -> bool:
    current = _bigrams(text)
    if not current:
        return False
    return any(_similar(text, previous) for previous in (_group_recent_replies.get(group_id) or [])[-8:])


def remember_bot_said(group_id: str, text: str) -> None:
    if not text or not group_id:
        return
    bucket = _group_recent_replies.setdefault(group_id, [])
    bucket.append(str(text)[:300])
    del bucket[:-10]


def get_talk_value(group_id: str) -> float:
    if group_id not in _group_talk_value:
        data = load_json(TALK_VALUE_FILE, {})
        try:
            _group_talk_value[group_id] = float(data.get(group_id, 1.0))
        except (TypeError, ValueError):
            _group_talk_value[group_id] = 1.0
    return _group_talk_value[group_id]


def set_talk_value(group_id: str, value: float) -> None:
    _group_talk_value[group_id] = max(0.2, min(3.0, float(value)))
    data = load_json(TALK_VALUE_FILE, {})
    data[group_id] = _group_talk_value[group_id]
    save_json(TALK_VALUE_FILE, data)


def dynamic_threshold(group_id: str) -> int:
    """按最近群活跃度和 /quiet /chatty 调整发言门槛。"""
    base = observe_threshold()
    now = time.time()
    recent = [item for item in _group_buffer.get(group_id, [])
              if now - float(item.get("ts", 0)) < 600]
    rate = len(recent) / 10.0  # 每分钟消息数
    if rate >= 8:
        base += 1
    elif rate <= 1.5:
        base -= 1
    value = get_talk_value(group_id)
    if value:
        base = base / value
    return max(3, min(9, int(round(base))))


def profile_key(group_id: str, user_id) -> str:
    return f"group_{group_id}:{user_id}"


def ask_profile(messages: list[dict]) -> list[str]:
    payload = json.dumps({"messages": messages}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        core_url() + "/api/profile",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.load(response)
        return [str(x) for x in (data.get("facts") or [])][:5]
    except Exception as exc:  # noqa: BLE001
        log_line(f"画像抽取失败 {type(exc).__name__} {exc}")
        return []


def update_profile(group_id: str, user_id, speaker: str, text: str, ts: float) -> None:
    key = profile_key(group_id, user_id)
    data = load_json(PROFILE_FILE, {})
    entry = dict(data.get(key) or {})
    entry["nickname"] = str(speaker)[:20]
    entry["messages"] = int(entry.get("messages", 0)) + 1
    entry["last"] = ts
    alias = re.search(r"叫我(.{1,12})", text)
    if alias:
        entry["alias"] = alias.group(1).strip("，。！？ ")
    facts = []
    for item in (entry.get("facts") or []):
        if isinstance(item, dict):
            facts.append(item)
        elif str(item).strip():
            facts.append({"text": str(item), "ts": ts, "weight": 1.0})
    decayed = []
    for fact in facts:
        age_days = max(0.0, (ts - float(fact.get("ts", ts) or ts)) / 86400)
        weight = float(fact.get("weight", 1.0) or 1.0) * (0.9 ** (age_days / 7))
        if age_days > 30 and weight < 0.4:
            continue
        fact["weight"] = round(weight, 3)
        decayed.append(fact)
    facts = decayed

    if entry["messages"] - int(entry.get("since_extract", 0)) >= 15:
        mine = [
            {"text": item.get("text", "")}
            for item in _group_buffer.get(group_id, [])
            if str(item.get("speaker")) == str(speaker)
        ][-12:]
        if mine:
            extracted = ask_profile(mine)
            for fact in extracted:
                match = next((f for f in facts if _similar(str(f.get("text")), fact)), None)
                if match:
                    match["ts"] = ts
                    match["weight"] = min(5.0, float(match.get("weight", 1.0)) + 0.5)
                else:
                    facts.append({"text": fact, "ts": ts, "weight": 1.0})
            log_line(f"[画像] {group_id} | {speaker} | 抽取 {len(extracted)} 条")
        entry["since_extract"] = entry["messages"]

    facts.sort(key=lambda item: float(item.get("weight", 0)), reverse=True)
    del facts[:-15]
    entry["facts"] = facts
    data[key] = entry
    save_json(PROFILE_FILE, data)


def build_profile_context(group_id: str) -> str:
    data = load_json(PROFILE_FILE, {})
    names = []
    for item in reversed(_group_buffer.get(group_id, [])[-20:]):
        name = item.get("speaker")
        if name and name not in names:
            names.append(name)
        if len(names) >= 3:
            break
    lines = []
    for name in names:
        for key, entry in data.items():
            if not key.startswith(f"group_{group_id}:") or entry.get("nickname") != name:
                continue
            facts = [
                str(x.get("text") if isinstance(x, dict) else x)
                for x in (entry.get("facts") or [])
            ][:3]
            alias = entry.get("alias")
            label = f"{name}（聊过 {entry.get('messages', 0)} 次" + (f"，想被叫 {alias}" if alias else "") + "）"
            if facts:
                label += "：" + "；".join(facts)
            lines.append(label)
            break
    return "\n".join(lines)


def group_persona(group_id: str) -> str:
    data = load_json(PERSONA_FILE, {})
    return str(data.get(group_id) or "").strip()


def ask_summary(session: str, messages: list[dict]) -> str:
    payload = json.dumps(
        {"session": session, "messages": messages}, ensure_ascii=False
    ).encode("utf-8")
    request = urllib.request.Request(
        core_url() + "/api/summarize",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.load(response)
        return str(data.get("summary") or "")
    except Exception as exc:  # noqa: BLE001
        log_line(f"摘要失败 {type(exc).__name__} {exc}")
        return ""


def ask_slang(messages: list[dict]) -> list[str]:
    payload = json.dumps({"messages": messages}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        core_url() + "/api/slang",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.load(response)
        return [str(x) for x in (data.get("terms") or [])][:5]
    except Exception as exc:  # noqa: BLE001
        log_line(f"黑话挖掘失败 {type(exc).__name__} {exc}")
        return []


def maybe_remember_group(group_id: str) -> None:
    """攒够消息就写一条摘要 / 挖一次黑话，都走观察额度。"""
    recent = [
        {"speaker": item.get("speaker", ""), "text": item.get("text", "")}
        for item in _group_buffer.get(group_id, [])[-24:]
    ]
    if len(recent) < 8:
        return
    counter = int(_group_summary_counter.get(group_id, 0)) + 1
    _group_summary_counter[group_id] = counter
    if counter >= 20:
        _group_summary_counter[group_id] = 0
        if observe_budget_ok():
            text = ask_summary(f"group_{group_id}", recent)
            if text:
                log_line(f"[摘要] {group_id} | {text[:80]}")

    slang_counter = int(_group_slang_counter.get(group_id, 0)) + 1
    _group_slang_counter[group_id] = slang_counter
    if slang_counter >= 60:
        _group_slang_counter[group_id] = 0
        if observe_budget_ok():
            terms = ask_slang(recent)
            if terms:
                data = load_json(SLANG_FILE, {})
                data[group_id] = terms
                save_json(SLANG_FILE, data)
                log_line(f"[黑话] {group_id} | {'、'.join(terms)}")


def slang_context(group_id: str) -> str:
    terms = load_json(SLANG_FILE, {}).get(group_id) or []
    return "【群里黑话】" + "、".join(str(x) for x in terms[:5]) if terms else ""


def build_group_context(group_id: str) -> str:
    """把没进会话历史的群聊带进本次回复，避免「6」这种短消息看不懂。"""
    history = list(_group_buffer.get(group_id, [])[-9:-1])
    lines = [
        f"{item.get('speaker', '')}：{item.get('text', '')}" for item in history
    ]
    last_bot = _group_last_bot_text.get(group_id, "")
    if last_bot:
        lines.append(f"我：{last_bot[:200]}")
    return "\n".join(lines)


def _history_message(raw: dict) -> tuple[str, str, str, str]:
    """从 OneBot 历史消息里取出 (昵称, 文本, QQ号, 消息id)。"""
    sender = raw.get("sender") or {}
    name = str(
        sender.get("card") or sender.get("nickname") or raw.get("user_id") or "群友"
    )[:20]
    chunks: list[str] = []
    for seg in (raw.get("message") or []):
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        data = seg.get("data") or {}
        if seg_type == "text":
            chunks.append(str(data.get("text") or ""))
        elif seg_type == "at":
            qq = str(data.get("qq") or "")
            chunks.append("@全体" if qq == "all" else ("@" + qq if qq else ""))
        elif seg_type == "image":
            chunks.append("[图片]")
    text = " ".join(part.strip() for part in chunks if part and part.strip())
    return name, text[:300], str(raw.get("user_id") or ""), str(raw.get("message_id") or "")


def fetch_group_history(group_id: str, limit: int | None = None) -> list[dict]:
    """尽量往回翻群历史。OneBot 一页只给 30 条，得用 message_seq 倒着取。"""
    want = min(limit if limit is not None else backfill_count(), 1000)
    if want <= 0:
        return []
    gid = int(group_id) if str(group_id).isdigit() else group_id
    collected: dict[str, dict] = {}
    seq: int | None = None
    for _ in range(40):
        payload: dict = {"group_id": gid, "count": 30}
        if seq is not None:
            payload["message_seq"] = seq
            payload["reverseOrder"] = True
        result = onebot_call("get_group_msg_history", payload)
        if not result:
            break
        messages = ((result.get("data") or {}).get("messages") or [])
        if not messages:
            break
        fresh = 0
        for raw in messages:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("message_id") or raw.get("message_seq") or "")
            if not key or key in collected:
                continue
            collected[key] = raw
            fresh += 1
        seqs = [
            int(item["message_seq"]) for item in messages
            if isinstance(item, dict) and item.get("message_seq") is not None
        ]
        if not seqs or fresh == 0 or len(collected) >= want:
            break
        seq = min(seqs)
    ordered = sorted(collected.values(), key=lambda item: float(item.get("time") or 0))
    return ordered[-want:]


def _sample_messages(items: list[dict], wanted: int = 24) -> list[dict]:
    """摘要只要抓重点，均匀取样比只取最后几条更能覆盖整段历史。"""
    picked: list[dict] = []
    for raw in items:
        name, text, uid, _ = _history_message(raw)
        if text and uid != cfg("QQ_BOT_QQ"):
            picked.append({"speaker": name, "text": text})
    if len(picked) <= wanted:
        return picked
    step = len(picked) / wanted
    return [picked[int(index * step)] for index in range(wanted)]


def seed_profiles(group_id: str, items: list[dict]) -> int:
    """回填时一次给几个主要说话人建画像，比逐条触发省调用。"""
    by_user: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    for raw in items:
        name, text, uid, _ = _history_message(raw)
        if not text or not uid or uid == cfg("QQ_BOT_QQ"):
            continue
        by_user.setdefault(uid, []).append({"text": text})
        names[uid] = name
    targets = sorted(by_user.items(), key=lambda kv: len(kv[1]), reverse=True)[:5]
    data = load_json(PROFILE_FILE, {})
    added = 0
    for uid, messages in targets:
        if len(messages) < 4 or not observe_budget_ok():
            continue
        facts = ask_profile(messages[-12:])
        if not facts:
            continue
        key = profile_key(group_id, uid)
        entry = dict(data.get(key) or {})
        entry["nickname"] = names.get(uid) or entry.get("nickname") or str(uid)
        entry["messages"] = max(int(entry.get("messages", 0)), len(messages))
        entry["last"] = time.time()
        stored = [x for x in (entry.get("facts") or []) if isinstance(x, dict)]
        for fact in facts:
            if any(_similar(str(x.get("text")), fact) for x in stored):
                continue
            stored.append({"text": fact, "ts": time.time(), "weight": 1.0})
            added += 1
        entry["facts"] = stored[-15:]
        data[key] = entry
    if added:
        save_json(PROFILE_FILE, data)
    return added


def refresh_group_intro(group_id: str) -> dict:
    """读群名、群公告、成员，给她一份「这是哪个群」的底稿。"""
    gid = int(group_id) if str(group_id).isdigit() else group_id
    info = ((onebot_call("get_group_info", {"group_id": gid, "no_cache": True}) or {}).get("data") or {})
    raw_members = (onebot_call("get_group_member_list", {"group_id": gid, "no_cache": True}) or {}).get("data") or []
    members: list[dict] = []
    for item in raw_members if isinstance(raw_members, list) else []:
        name = str(item.get("card") or item.get("nickname") or item.get("user_id") or "")[:20]
        if not name:
            continue
        members.append({"name": name, "role": str(item.get("role") or "member")})
    owner = next((m["name"] for m in members if m["role"] == "owner"), "")
    admins = [m["name"] for m in members if m["role"] == "admin"][:4]
    entry = {
        "group_id": str(group_id),
        "name": str(info.get("group_name") or ""),
        "notice": str(info.get("group_memo") or info.get("group_notice") or "")[:300],
        "count": len(members) or int(info.get("member_count") or 0),
        "created": int(info.get("group_create_time") or 0),
        "owner": owner,
        "admins": admins,
        "members": [m["name"] for m in members[:20]],
        "ts": time.time(),
    }
    data = load_json(INTRO_FILE, {})
    data[str(group_id)] = entry
    save_json(INTRO_FILE, data)
    return entry


def intro_context(group_id: str) -> str:
    entry = (load_json(INTRO_FILE, {}) or {}).get(str(group_id)) or {}
    if not entry:
        return ""
    head = (
        f"【这个群】{entry.get('name') or '未命名'}"
        f"（群号 {group_id}，{entry.get('count') or '?'} 人"
    )
    created = int(entry.get("created") or 0)
    if created:
        head += f"，建于 {time.strftime('%Y-%m-%d', time.localtime(created))}"
    head += "）"
    lines = [head]
    if entry.get("notice"):
        lines.append("【群公告】" + str(entry["notice"]))
    people = []
    if entry.get("owner"):
        people.append("群主 " + str(entry["owner"]))
    if entry.get("admins"):
        people.append("管理 " + "、".join(str(x) for x in entry["admins"]))
    if entry.get("members"):
        people.append("常客 " + "、".join(str(x) for x in entry["members"][:10]))
    if people:
        lines.append("【群里的人】" + "；".join(people))
    return "\n".join(lines)


def backfill_group(
    group_id: str, limit: int | None = None, force: bool = False, warmup: bool | None = None
) -> dict:
    """进群或第一次见到某个群时，把能拿到的历史灌进上下文、画像和摘要。

    warmup 为 None 时只在第一次见到这个群才潜水，重启后的补数据不会让她闭嘴。
    """
    group_id = str(group_id)
    now = time.time()
    with _observe_lock:
        done_at = float(_backfilled.get(group_id, 0.0))
        if done_at and not force and now - done_at < 6 * 3600:
            return {"ok": False, "reason": "六小时内补过", "group_id": group_id}
        first_time = not done_at
        _backfilled[group_id] = now
        if warmup is None:
            warmup = first_time
        if warmup:
            _group_warmup[group_id] = now + group_warmup()
    save_json(BACKFILL_FILE, {gid: int(ts) for gid, ts in _backfilled.items()})

    intro = refresh_group_intro(group_id)
    items = fetch_group_history(group_id, limit)
    added = 0
    if items:
        known = {
            (str(item.get("speaker")), str(item.get("text")))
            for item in _group_buffer.get(group_id, [])
        }
        bot_qq = cfg("QQ_BOT_QQ")
        for raw in items:
            name, text, uid, msg_id = _history_message(raw)
            if not text or (bot_qq and uid == bot_qq):
                continue
            if (name, text) in known:
                continue
            record_group_message(
                group_id, name, text, float(raw.get("time") or time.time()), msg_id
            )
            known.add((name, text))
            added += 1
        if added:
            with _observe_lock:
                buffer = _group_buffer.get(group_id, [])
                buffer.sort(key=lambda item: float(item.get("ts") or 0))
                for index, item in enumerate(buffer, start=1):
                    item["seq"] = index
                _group_seq[group_id] = len(buffer)
                _group_cursor[group_id] = len(buffer)
            facts = seed_profiles(group_id, items)
            sample = _sample_messages(items)
            if len(sample) >= 8 and observe_budget_ok():
                summary = ask_summary(f"group_{group_id}", sample)
                if summary:
                    log_line(f"[回填摘要] {group_id} | {summary[:80]}")
            log_line(f"[回填画像] {group_id} | 新增 {facts} 条事实")
    log_line(
        f"[回填] {group_id} | {intro.get('name') or '未知群'} | "
        f"取到 {len(items)} 条，入上下文 {added} 条"
    )
    return {
        "ok": True,
        "group_id": group_id,
        "name": intro.get("name"),
        "fetched": len(items),
        "added": added,
        "warmup": int(group_warmup()),
    }


def on_bot_joined(group_id: str) -> None:
    """自己被拉进群的完整流程：补历史 → 读群资料 → 空群就打个招呼。"""
    result = backfill_group(group_id, warmup=True)
    if not result.get("ok"):
        log_line(f"[进群] {group_id} | 没补成：{result.get('reason')}")
        return
    if not join_greet_enabled():
        return
    # 老群有历史，进去就说话很突兀；只有基本没人说过话的新群才打招呼
    if int(result.get("fetched") or 0) >= 5:
        log_line(f"[进群] {group_id} | 群里有历史，先潜水")
        return
    reply = ask_event("你刚被拉进一个还没人说话的新群", intro_context(group_id))
    if not reply:
        return
    parts = split_reply(clean_for_group(reply))
    if not parts:
        return
    ok = send_group_parts(group_id, parts)
    remember_bot_said(group_id, reply)
    with _observe_lock:
        _group_last_reply[group_id] = time.time()
        _group_last_bot_text[group_id] = "\n".join(parts)
    log_line(f"[进群] {group_id} | 打招呼：{reply[:100]} | sent={ok}")


def ask_observe(messages: list[dict], probe: str = "", intro: str = "") -> dict:
    payload = json.dumps(
        {"messages": messages, "probe": probe, "intro": intro}, ensure_ascii=False
    ).encode("utf-8")
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
            "addressed": False,
            "speak": False,
            "reply": "",
            "reason": f"评估接口异常：{type(exc).__name__} {exc}",
        }


def looks_like_followup(text: str) -> bool:
    """像接话的短追问才值得再花一次 lite 调用。"""
    text = (text or "").strip()
    if not text or len(text) > 60:
        return False
    if "?" in text or "？" in text:
        return True
    starters = ("所以", "那", "那么", "你", "怎么", "为什么", "哪儿", "哪里",
                "几点", "真的", "嗯", "哦", "行吧", "好了", "说完了", "还有")
    return any(text.startswith(word) for word in starters)


def probe_context(group_id: str, speaker: str, text: str) -> dict | None:
    """没被 @ 时，结合上下文判断这句话是不是在跟她说话。"""
    if not context_probe_enabled() or not looks_like_followup(text):
        return None
    now = time.time()
    if now - float(_group_last_probe.get(group_id, 0.0)) < context_probe_gap():
        return None
    last_reply = float(_group_last_reply.get(group_id, 0.0))
    if not last_reply or now - last_reply > context_probe_window():
        return None
    if not observe_budget_ok():
        return None
    _group_last_probe[group_id] = now

    history = list(_group_buffer.get(group_id, [])[-10:-1])
    last_bot_text = _group_last_bot_text.get(group_id, "")
    if last_bot_text:
        history.append({"speaker": "我（Gemini娘）", "text": last_bot_text[:300]})
    decision = ask_observe(history, probe=f"{speaker}：{text}")
    log_line(
        f"[上下文判定] {group_id} | {speaker} | {text[:40]} | "
        f"addressed={decision.get('addressed')} 激活度={decision.get('activation')} "
        f"| {decision.get('reason')}"
    )
    if decision.get("addressed") and decision.get("reply"):
        return decision
    return None


def send_contextual_reply(group_id: str, user_id, decision: dict) -> dict:
    reply = str(decision.get("reply") or "")
    if is_duplicate(group_id, reply):
        log_line(f"[上下文去重] {group_id} | {reply[:60]}")
        return {"ok": False, "reason": "重复回复已跳过"}
    parts = split_reply(clean_for_group(reply))
    if not parts:
        return {"ok": False, "reason": "上下文回复为空"}
    ok = send_group_parts(group_id, parts)
    now = time.time()
    _group_last_reply[group_id] = now
    _group_last_bot_text[group_id] = "\n".join(parts)
    remember_bot_said(group_id, reply)
    mark_consumed(group_id)
    log_line(
        f"[上下文回复] {group_id} | 激活度 {decision.get('activation')} | "
        f"{reply[:120]}"
    )
    return {
        "ok": True,
        "reason": "上下文指向她",
        "session": f"group_{group_id}",
        "reply": reply,
        "parts": parts,
        "sent": ok,
        "dry_run": is_dry(),
        "simulated": True,
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


def ask_core(
    text: str, session: str, channel: str, speaker: str,
    profile: str = "", persona_override: str = "", context: str = "",
) -> tuple[str, str | None]:
    payload = json.dumps(
        {
            "message": text, "session": session, "channel": channel,
            "speaker": speaker, "profile": profile,
            "persona_override": persona_override, "context": context,
        },
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


def onebot_call(action: str, payload: dict) -> dict | None:
    """调 OneBot 接口并返回原始响应，失败返回 None。查询类接口要用它。"""
    api = onebot_api()
    if not api:
        return None
    request = urllib.request.Request(
        f"{api}/{action}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except Exception as exc:  # noqa: BLE001
        log_line(f"调用失败 {action}: {type(exc).__name__} {exc}")
        return None
    if isinstance(result, dict) and result.get("retcode") not in (0, None):
        log_line(f"调用失败 {action}: {result}")
        return None
    return result if isinstance(result, dict) else None


def call_onebot(action: str, payload: dict) -> tuple[bool, str | None]:
    """返回 (是否成功, OneBot 返回的 message_id)。"""
    if is_dry():
        print(f"[干跑] {action} {json.dumps(payload, ensure_ascii=False)[:300]}", flush=True)
        return True, None
    result = onebot_call(action, payload)
    if result is None:
        return False, None
    data = result.get("data") or {}
    message_id = None
    if isinstance(data, dict) and data.get("message_id") is not None:
        message_id = str(data["message_id"])
    return True, message_id


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
    if not is_dry() and sticker_rate() > 0 and random.random() < sticker_rate():
        stickers = sorted((ROOT / "static" / "expressions").glob("*.png"))
        if stickers:
            encoded = base64.b64encode(random.choice(stickers).read_bytes()).decode("ascii")
            messages[-1].append(
                {"type": "image", "data": {"file": f"base64://{encoded}"}}
            )
            log_line(f"[表情] {group_id} | 随消息附了一张表情差分")
    with _reply_lock:
        for index, segments in enumerate(messages):
            if index:
                time.sleep(0.8)
            ok, message_id = call_onebot(
                "send_group_msg", {"group_id": int(group_id), "message": segments}
            )
            if not ok and any(seg.get("type") == "at" for seg in segments):
                fallback = [seg for seg in segments if seg.get("type") != "at"]
                ok, message_id = call_onebot(
                    "send_group_msg", {"group_id": int(group_id), "message": fallback}
                )
            if not ok:
                return False
            # 记下自己的消息 id，别人回复她时才能认出来
            remember_bot_message(message_id)
    return True


def ask_event(event_text: str, context: str) -> str:
    payload = json.dumps(
        {"event": event_text, "context": context}, ensure_ascii=False
    ).encode("utf-8")
    request = urllib.request.Request(
        core_url() + "/api/event",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.load(response)
        return str(data.get("reply") or "")
    except Exception as exc:  # noqa: BLE001
        log_line(f"群事件回应失败 {type(exc).__name__} {exc}")
        return ""


def handle_notice(event: dict) -> dict:
    """撤回 / 入群 / 退群这类群事件，偶尔让她自然接一句。"""
    group_id = str(event.get("group_id", ""))
    notice_type = event.get("notice_type")
    join_types = ("group_increase", "group_member_increase", "group_member_join")

    # 自己被拉进新群：先把能拿到的历史补上，新群也要先看一会儿再说话
    # 自己被拉进新群：补历史、读群资料，空群再打个招呼
    if notice_type in join_types and str(event.get("user_id")) == cfg("QQ_BOT_QQ") and cfg("QQ_BOT_QQ"):
        threading.Thread(target=on_bot_joined, args=(group_id,), daemon=True).start()
        log_line(f"[进群] {group_id} | 自己被拉进群，开始补历史")
        return {"ok": True, "reason": "自己进群，开始补历史", "group_id": group_id}

    if not group_allowed(group_id):
        return {"ok": False, "reason": "群不在白名单"}
    if notice_type not in (
        "group_recall", *join_types, "group_decrease", "group_member_decrease", "group_member_leave",
    ):
        return {"ok": False, "reason": "无关通知"}
    user_id = event.get("user_id") or event.get("operator_id")
    if str(user_id) == cfg("QQ_BOT_QQ"):
        return {"ok": False, "reason": "自己的事件"}

    now = time.time()
    if now - float(_group_last_notice.get(group_id, 0.0)) < notice_gap():
        return {"ok": False, "reason": "群事件冷却中"}
    if random.random() >= notice_rate():
        return {"ok": False, "reason": "这次不理"}
    _group_last_notice[group_id] = now

    who = str(user_id or "有人")
    texts = {
        "group_recall": f"{who}撤回了一条消息",
        "group_member_join": f"{who}进了群",
        "group_increase": f"{who}进了群",
        "group_member_increase": f"{who}进了群",
        "group_member_leave": f"{who}退了群",
        "group_decrease": f"{who}退了群",
        "group_member_decrease": f"{who}退了群",
    }
    event_text = texts[notice_type]
    reply = ask_event(event_text, build_group_context(group_id))
    if not reply or is_duplicate(group_id, reply):
        return {"ok": False, "reason": "没什么好说的"}
    parts = split_reply(clean_for_group(reply))
    if not parts:
        return {"ok": False, "reason": "空回复"}
    ok = send_group_parts(group_id, parts)
    remember_bot_said(group_id, reply)
    log_line(f"[群事件] {group_id} | {event_text} | {reply[:120]}")
    return {
        "ok": True, "reason": "群事件回应", "reply": reply,
        "parts": parts, "sent": ok,
    }


def handle_request(event: dict) -> dict:
    """有人邀请入群：只接受管理员的邀请，并自动同意。"""
    if str(event.get("request_type")) != "group":
        return {"ok": False, "reason": "非群请求"}
    group_id = str(event.get("group_id", ""))
    sub_type = str(event.get("sub_type") or "")
    if sub_type not in ("invite", "add"):
        return {"ok": False, "reason": "不是邀请入群"}
    inviter = str(event.get("user_id") or "")
    if inviter not in admin_qq():
        log_line(f"[入群邀请] {group_id} | 邀请人 {inviter} 不是管理员，忽略")
        return {"ok": False, "reason": "邀请人不是管理员"}
    if not auto_accept_invite():
        log_line(f"[入群邀请] {group_id} | 自动同意已关闭")
        return {"ok": False, "reason": "自动同意已关闭"}
    flag = str(event.get("flag") or "")
    if not flag:
        return {"ok": False, "reason": "缺少 flag"}
    ok, _ = call_onebot(
        "set_group_add_request",
        {"flag": flag, "sub_type": sub_type, "approve": True},
    )
    log_line(f"[入群邀请] {group_id} | 邀请人 {inviter} | 已同意={ok}")
    return {
        "ok": bool(ok),
        "reason": "已同意入群" if ok else "同意失败",
        "group_id": group_id,
    }


def handle_group_command(group_id: str, user_id, text: str) -> dict | None:
    """群内运维指令：只有管理员能改发言频率。"""
    if str(user_id) not in admin_qq():
        return None
    text = (text or "").strip()
    if text.startswith("/quiet"):
        set_talk_value(group_id, 0.4)
        reply = "好，我安静点。"
    elif text.startswith("/chatty"):
        set_talk_value(group_id, 2.0)
        reply = "行，那我多说两句。"
    elif text.startswith("/normal"):
        set_talk_value(group_id, 1.0)
        reply = "恢复平常。"
    elif text.startswith("/人设"):
        persona = text[3:].strip()
        data = load_json(PERSONA_FILE, {})
        if persona in ("清除", "默认", "reset"):
            data.pop(group_id, None)
            reply = "这个群的人设恢复默认了。"
        elif persona:
            data[group_id] = persona[:2000]
            reply = "记住了，这个群就按这个人设来。"
        else:
            return None
        save_json(PERSONA_FILE, data)
    elif text.startswith("/忘掉"):
        keyword = text[3:].strip()
        if not keyword:
            return None
        data = load_json(PROFILE_FILE, {})
        removed = 0
        for key, entry in data.items():
            if not key.startswith(f"group_{group_id}:"):
                continue
            kept = []
            for fact in (entry.get("facts") or []):
                value = str(fact.get("text") if isinstance(fact, dict) else fact)
                if keyword in value:
                    removed += 1
                else:
                    kept.append(fact)
            entry["facts"] = kept
        save_json(PROFILE_FILE, data)
        reply = f"行，忘掉了 {removed} 条。"
    elif text.startswith("/记住"):
        body = text[3:].strip()
        if "：" not in body and ":" not in body:
            return None
        separator = "：" if "：" in body else ":"
        nickname, fact = body.split(separator, 1)
        nickname, fact = nickname.strip()[:20], fact.strip()[:80]
        if not nickname or not fact:
            return None
        data = load_json(PROFILE_FILE, {})
        target = None
        for key, entry in data.items():
            if key.startswith(f"group_{group_id}:") and entry.get("nickname") == nickname:
                target = entry
                break
        if target is None:
            target = {"nickname": nickname, "facts": [], "messages": 0, "since_extract": 0}
            data[profile_key(group_id, f"manual:{nickname}")] = target
        current = target.setdefault("facts", [])
        if not any(_similar(str(f.get("text") if isinstance(f, dict) else f), fact) for f in current):
            current.append({"text": fact, "ts": time.time(), "weight": 1.5})
        save_json(PROFILE_FILE, data)
        reply = f"记住了：{nickname} —— {fact}"
    elif text.startswith(("/回填", "/补历史")):
        result = backfill_group(group_id, force=True)
        if result.get("ok"):
            reply = (
                f"翻了 {result.get('fetched', 0)} 条，进了 {result.get('added', 0)} 条上下文。"
                f"这个群叫「{result.get('name') or '不知道'}」。"
            )
        else:
            reply = f"没补成：{result.get('reason')}"
    elif text.startswith(("/群资料", "/群信息")):
        entry = refresh_group_intro(group_id)
        reply = (
            f"群名「{entry.get('name') or '未知'}」，{entry.get('count') or '?'} 人"
        )
        if entry.get("owner"):
            reply += f"，群主 {entry['owner']}"
        if entry.get("notice"):
            reply += f"。公告：{entry['notice'][:60]}"
    else:
        return None
    send_group_parts(group_id, [reply])
    log_line(f"[指令] {group_id} | {user_id} | {text} | {reply}")
    return {
        "ok": True,
        "reason": "群内指令",
        "session": f"group_{group_id}",
        "reply": reply,
        "parts": [reply],
        "command": text,
    }


def handle_event(event: dict, simulated: bool = False) -> dict:
    """返回处理结果，便于模拟器直接看到判定过程。"""
    if event.get("post_type") == "request":
        return handle_request(event)
    if event.get("post_type") == "notice":
        return handle_notice(event)
    if event.get("post_type") not in (None, "message"):
        return {"ok": False, "reason": "非消息事件"}

    message_type = event.get("message_type")
    if message_type not in ("group", "private"):
        return {"ok": False, "reason": "未知消息类型"}

    text, at_self, reply_id, images = parse_message(event.get("message"))
    if images:
        text = (text + " [对方发了图片]").strip()
    silent_ping = not text and (at_self or is_reply_to_bot(reply_id))
    if not text:
        if silent_ping:
            text = "（只是叫了你一下，没说话）"
        else:
            return {"ok": False, "reason": "空消息"}

    group = message_type == "group"
    group_id = str(event.get("group_id", ""))
    user_id = event.get("user_id")
    speaker = display_name(event)
    if group:
        command = handle_group_command(group_id, user_id, text)
        if command is not None:
            return command
        log_line(
            f"[入站] {group_id} | {display_name(event)} | {text[:60]} | "
            f"at={at_self} reply={reply_id}"
        )
    # 不回复也记下来，观察循环要靠这些上下文判断气氛
    if group and group_id and str(event.get("user_id")) != cfg("QQ_BOT_QQ"):
        record_group_message(
            group_id, display_name(event), text, msg_id=str(event.get("message_id") or "")
        )
        update_profile(group_id, user_id, speaker, text, time.time())
        maybe_remember_group(group_id)
        # 第一次见到这个群（比如刚被拉进来）：后台补历史 + 读群资料
        if group_id not in _backfilled and group_allowed(group_id):
            threading.Thread(target=backfill_group, args=(group_id,), daemon=True).start()

    reply_to_bot = is_reply_to_bot(reply_id)
    engage, reason = should_engage(event, text, at_self, reply_to_bot)
    if not engage:
        if group:
            decision = probe_context(group_id, speaker, text)
            if decision:
                return send_contextual_reply(group_id, user_id, decision)
        log_line(f"[跳过] {group_id} | {reason}")
        return {"ok": False, "reason": reason}

    session = f"group_{group_id}" if group else f"private_{user_id}"
    channel = "group" if group else "private"

    now = time.time()
    if group:
        direct = reason in ("被@", "回复了她", "叫了名字", "私聊白名单")
        limit = direct_cooldown_seconds() if direct else cooldown_seconds()
        elapsed = now - float(_group_last_reply.get(group_id, 0.0))
        if elapsed < limit:
            if direct:
                # 被直接叫到时不吞消息，等完很短的间隔再回
                time.sleep(min(limit - elapsed, 10))
            else:
                log_line(f"[跳过] {group_id} | 冷却中")
                return {"ok": False, "reason": "冷却中"}
    if group and event.get("message_id") is not None:
        reply_id = str(event.get("message_id"))

    if not simulated and not is_dry() and not budget_ok():
        log_line(f"预算用完，跳过 {session}")
        return {"ok": False, "reason": "今日预算用完"}

    prompt = f"{speaker}：{text}" if group else text
    if group and (silent_ping or len(text) <= 6):
        earlier = build_group_context(group_id)
        if earlier:
            prompt = (
                f"【群里刚说过，下面这句是在接这些话】\n{earlier}\n\n"
                f"{speaker}（接着上面的话）：{text}\n"
                "（请回答上面那句还没被回应的话，不要只评价这句短消息。）"
            )
    profile_context = build_profile_context(group_id) if group else ""
    if group:
        intro = intro_context(group_id)
        if intro:
            profile_context = (intro + "\n" + profile_context).strip()
    slang = slang_context(group_id) if group else ""
    if slang:
        profile_context = (profile_context + "\n" + slang).strip()
    persona = group_persona(group_id) if group else ""
    group_context = build_group_context(group_id) if group else ""
    with _reply_lock:
        reply, error = ask_core(
            prompt, session, channel, speaker,
            profile_context, persona, group_context,
        )

    if error or not reply:
        log_line(f"{session} | {speaker} | {text} | 调用失败: {error}")
        return {"ok": False, "reason": f"核心调用失败: {error}"}

    if group and is_duplicate(group_id, reply):
        log_line(f"[去重] {group_id} | 原回复太像：{reply[:60]}")
        retry_prompt = prompt + "\n（刚才你说过很相近的话，这次换个说法，别重复。）"
        with _reply_lock:
            retry, retry_error = ask_core(
                retry_prompt, session, channel, speaker,
                profile_context, persona, group_context,
            )
        if retry and not retry_error:
            reply = retry

    parts = split_reply(clean_for_group(reply) if group else reply)
    if group:
        ok = send_group_parts(group_id, parts, reply_id, user_id)
    else:
        ok = True
        with _reply_lock:
            for index, part in enumerate(parts):
                if index:
                    time.sleep(0.8)
                sent, message_id = call_onebot(
                    "send_private_msg",
                    {
                        "user_id": int(user_id),
                        "message": [{"type": "text", "data": {"text": part}}],
                    },
                )
                if not sent:
                    ok = False
                    break
                remember_bot_message(message_id)

    if group:
        _group_last_reply[group_id] = time.time()
        _group_last_bot_text[group_id] = "\n".join(parts)
        remember_bot_said(group_id, reply)
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
    if not group_id or not group_allowed(group_id):
        return {"ok": False, "spoke": False, "reason": "群不在白名单"}
    if not force and not observe_enabled():
        return {"ok": False, "spoke": False, "reason": "观察未开启"}
    if not force and time.time() < float(_group_warmup.get(group_id, 0.0)):
        return {"ok": False, "spoke": False, "reason": "刚进群，先潜水"}

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
        [{"speaker": item.get("speaker", ""), "text": item.get("text", "")} for item in window],
        intro=intro_context(group_id),
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
    threshold = dynamic_threshold(group_id)
    result["threshold"] = threshold
    if decision.get("speak") and activation >= threshold and decision.get("reply"):
        candidate = str(decision["reply"])
        if is_duplicate(group_id, candidate):
            log_line(f"group_{group_id} | 主动发言去重 | {candidate[:60]}")
            log_line(f"group_{group_id} | 观察后沉默 | 激活度 {activation}/10 | 重复回复")
            return result
        parts = split_reply(clean_for_group(candidate))
        if parts:
            ok = send_group_parts(group_id, parts)
            result["spoke"] = True
            result["reply"] = str(decision["reply"])
            result["parts"] = parts
            result["sent"] = ok
            with _observe_lock:
                _group_last_observe[group_id] = now
                _group_last_reply[group_id] = now
                _group_last_bot_text[group_id] = "\n".join(parts)
            remember_bot_said(group_id, candidate)
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
        for group_id in sorted(known_groups()):
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
        raw = b""
        try:
            if "chunked" in str(self.headers.get("Transfer-Encoding", "")).lower():
                while True:
                    line = self.rfile.readline().strip()
                    size = int(line.split(b";")[0], 16)
                    if size == 0:
                        self.rfile.readline()
                        break
                    raw += self.rfile.read(size)
                    self.rfile.read(2)
            else:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > 0:
                    raw = self.rfile.read(length)
            if not raw:
                return {}
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except Exception as exc:  # noqa: BLE001
            log_line(f"上报解析失败 {type(exc).__name__} {exc} | {raw[:300]!r}")
            return {}

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(
                {
                    "ok": True,
                    "core": core_url(),
                    "onebot": onebot_api() or "dry-run",
                    "groups": sorted(known_groups()),
                    "private": sorted(private_whitelist()),
                    "interject": interject_probability(),
                    "cooldown": cooldown_seconds(),
                    "observe": observe_enabled(),
                    "observe_threshold": observe_threshold(),
                    "observe_cooldown": int(observe_cooldown()),
                    "observe_interval": int(observe_interval()),
                    "observe_min_messages": observe_min_messages(),
                    "observe_daily_calls": observe_daily_calls(),
                    "context_probe": context_probe_enabled(),
                    "backfill": {
                        "count": backfill_count(),
                        "buffer_max": buffer_max(),
                        "warmup": int(group_warmup()),
                    },
                    "invite": {
                        "auto_accept": auto_accept_invite(),
                        "greet": join_greet_enabled(),
                    },
                    "talk_value": {gid: get_talk_value(gid) for gid in sorted(known_groups())},
                    "threshold": {gid: dynamic_threshold(gid) for gid in sorted(known_groups())},
                    "admin": sorted(admin_qq()),
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
                            "talk_value": get_talk_value(gid),
                            "threshold": dynamic_threshold(gid),
                            "recent_replies": len(_group_recent_replies.get(gid, [])),
                            "profiles": len([
                                k for k in load_json(PROFILE_FILE, {})
                                if k.startswith(f"group_{gid}:")
                            ]),
                            "persona": bool(group_persona(gid)),
                            "intro": bool(intro_context(gid)),
                            "backfilled_at": int(_backfilled.get(gid, 0)),
                            "warmup_left": max(0, int(_group_warmup.get(gid, 0) - time.time())),
                        }
                        for gid in sorted(known_groups())
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
            segments: list[dict] = []
            if body.get("remember_id") is not None:
                # 干跑时没有真实 message_id，先登记一个假的供测试
                remember_bot_message(body["remember_id"])
            if body.get("reply_id") is not None:
                segments.append(
                    {"type": "reply", "data": {"id": str(body["reply_id"])}}
                )
            segments.append(
                {"type": "text", "data": {"text": body.get("text", "")}}
            )
            event = {
                "post_type": "message",
                "message_type": body.get("message_type", "group"),
                "group_id": body.get("group_id", "10000"),
                "user_id": body.get("user_id", "20000"),
                "message_id": body.get("message_id", str(int(time.time()))),
                "sender": {"nickname": body.get("nickname", "测试群友")},
                "message": segments,
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
                segments: list[dict] = []
                if item.get("remember_id") is not None:
                    remember_bot_message(item["remember_id"])
                if item.get("reply_id") is not None:
                    segments.append(
                        {"type": "reply", "data": {"id": str(item["reply_id"])}}
                    )
                segments.append(
                    {"type": "text", "data": {"text": str(item.get("text") or "")}}
                )
                event = {
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": group_id,
                    "user_id": item.get("user_id", 20000 + index),
                    "message_id": msg_id,
                    "sender": {"nickname": str(item.get("nickname") or f"群友{index + 1}")[:20]},
                    "message": segments,
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
    host = bridge_host()
    stored = load_json(BACKFILL_FILE, {}) or {}
    for gid, ts in stored.items():
        try:
            _backfilled[str(gid)] = float(ts)
        except (TypeError, ValueError):
            continue
    print(f"QQ 桥已启动：http://{host}:{port}", flush=True)
    print(f"核心：{core_url()}", flush=True)
    print(f"OneBot：{onebot_api() or '未配置（干跑模式，只打印不发送）'}", flush=True)
    print(f"群白名单：{sorted(group_whitelist()) or '空（不会回任何群）'}", flush=True)
    print(f"私聊白名单：{sorted(private_whitelist()) or '空（不会回任何私聊）'}", flush=True)
    print(
        f"群聊观察：{'开' if observe_enabled() else '关'} | "
        f"阈值 {observe_threshold()}/10 | 发言后冷却 {int(observe_cooldown())} 秒",
        flush=True,
    )
    print(
        f"历史回填：最多 {backfill_count()} 条 | 缓冲 {buffer_max()} 条 | "
        f"新群潜水 {int(group_warmup())} 秒",
        flush=True,
    )
    if observe_enabled():
        threading.Thread(target=observe_loop, daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
