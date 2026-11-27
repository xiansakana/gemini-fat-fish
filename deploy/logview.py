#!/usr/bin/env python3
"""Gemini 娘后台日志页：只读、带 token、自动刷新。

挂在 http://<服务器IP>:8770/?token=xxx 上，能看桥、核心、NapCat、自动更新四路日志，
不提供任何写操作，也不暴露 .env 或会话内容。
"""

from __future__ import annotations

import glob
import hmac
import json
import os
import re
import shutil
import subprocess
import urllib.request
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("FISH_ROOT") or (
    os.path.dirname(SCRIPT_DIR) if os.path.basename(SCRIPT_DIR) == "deploy" else SCRIPT_DIR
)
LOG_DIR = os.path.join(ROOT, "logs")


def load_env(path: str) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                data[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return data


ENV = load_env(os.path.join(ROOT, ".env"))
TOKEN = os.environ.get("LOGVIEW_TOKEN") or ENV.get("LOGVIEW_TOKEN") or ""
PORT = int(os.environ.get("LOGVIEW_PORT") or ENV.get("LOGVIEW_PORT") or "8770")
HOST = os.environ.get("LOGVIEW_HOST") or ENV.get("LOGVIEW_HOST") or "0.0.0.0"


def tail_file(path: str, want: int) -> list[str]:
    """从文件尾部往前读，避免把几百 MB 的日志整个读进内存。"""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            data = b""
            block = 64 * 1024
            while end > 0 and data.count(b"\n") <= want:
                step = min(block, end)
                end -= step
                handle.seek(end)
                data = handle.read(step) + data
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    return [ANSI_RE.sub("", line) for line in lines][-want:]


def tail_many(paths: list[str], want: int) -> list[str]:
    lines: list[str] = []
    for path in reversed([item for item in paths if os.path.exists(item)]):
        if len(lines) >= want:
            break
        lines = tail_file(path, want - len(lines)) + lines
    return lines[-want:]


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def run(cmd: list[str], timeout: int = 15) -> list[str]:
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    text = b"\n".join(part for part in (done.stdout, done.stderr) if part)
    raw = text.decode("utf-8", "replace")
    return [ANSI_RE.sub("", line) for line in raw.splitlines()]


def bridge_log_files() -> list[str]:
    return sorted(glob.glob(os.path.join(LOG_DIR, "qq-*.log")))[-3:]


SOURCES = {
    "bridge": ("群消息", lambda n: tail_many(bridge_log_files(), n)),
    "bridge-out": ("桥 stdout", lambda n: tail_file(os.path.join(LOG_DIR, "bridge.log"), n)),
    "bridge-err": ("桥 stderr", lambda n: tail_file(os.path.join(LOG_DIR, "bridge.err.log"), n)),
    "core": ("核心 journal", lambda n: run(["journalctl", "-u", "gemini-fish", "-n", str(n), "--no-pager", "-o", "short-iso"])),
    "core-out": ("核心 stdout", lambda n: tail_file(os.path.join(LOG_DIR, "server.log"), n)),
    "core-err": ("核心 stderr", lambda n: tail_file(os.path.join(LOG_DIR, "server.err.log"), n)),
    "napcat": ("NapCat", lambda n: run(["docker", "logs", "napcat", "--tail", str(n), "--timestamps"])),
    "update": ("自动更新", lambda n: run(["journalctl", "-u", "gemini-fish-update", "-n", str(n), "--no-pager", "-o", "short-iso"])),
}


def status_snapshot() -> dict:
    units = ["gemini-fish", "gemini-fish-qq", "gemini-fish-logview", "gemini-fish-update.timer"]
    states = {}
    for unit in units:
        states[unit] = (run(["systemctl", "is-active", unit], timeout=5) or ["unknown"])[0]
    container = (run(["docker", "inspect", "-f", "{{.State.Status}}", "napcat"], timeout=5) or ["unknown"])[0]
    health = run(["curl", "-s", "--max-time", "5", "http://127.0.0.1:8767/health"], timeout=8)
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "units": states,
        "napcat": container,
        "bridge_health": "".join(health)[:4000],
        "log_files": {
            os.path.basename(path): os.path.getsize(path)
            for path in sorted(glob.glob(os.path.join(LOG_DIR, "*.log")))[-8:]
            if os.path.exists(path)
        },
    }



# ------------------------------ 配置页 ------------------------------
# 只有列在 CONFIG_FIELDS 里的 key 才会被读到 / 写到；
# GEMINI_API_KEY、LOGVIEW_TOKEN、QQ_ADMIN_QQ、QQ_BOT_QQ 一律不经过网页。
ENV_PATH = os.path.join(ROOT, ".env")
TALK_VALUE_PATH = os.path.join(ROOT, "data", "qq_talk_value.json")
ENV_BACKUP = os.path.join(ROOT, ".env.bak")
BOOL_ON = ("1", "true", "yes", "on")
BOOL_OFF = ("0", "false", "no", "off")


def field(key, label, kind, group, default, help="", **kw):
    item = {"key": key, "label": label, "kind": kind, "group": group,
            "default": str(default), "help": help}
    item.update(kw)
    return item


CONFIG_GROUPS = [
    {"id": "groups", "label": "群与白名单", "open": True,
     "hint": "拉进新群默认自动放行，不用再手改 .env"},
    {"id": "chatty", "label": "话痨与开口门槛", "open": True,
     "hint": "决定她多爱说话、多久评估一次"},
    {"id": "shape", "label": "回复形态", "open": False,
     "hint": "短句、拆条、打字节奏、表情概率"},
    {"id": "budget", "label": "额度与预算", "open": False,
     "hint": "每日调用上限，防止话痨烧太多额度"},
    {"id": "model", "label": "模型与思考", "open": False,
     "hint": "群聊 / 观察用的模型和输出长度"},
    {"id": "misc", "label": "视觉与上下文", "open": False,
     "hint": "看图、补历史、记忆缓冲"},
]

CONFIG_FIELDS = [
    # 群与白名单
    field("QQ_GROUP_WHITELIST", "群白名单", "list", "groups", "",
          "逗号分隔的群号；填 * 表示所有群都生效"),
    field("QQ_AUTO_WHITELIST", "新群自动放行", "bool", "groups", "1",
          "被拉进新群时自动把群号写进白名单，省得每次手动配置"),
    field("QQ_PRIVATE_WHITELIST", "私聊白名单", "list", "groups", "",
          "允许私聊的 QQ 号，留空表示不限制"),
    field("QQ_WAKE_WORDS", "唤醒词", "list", "groups", "小双,Gemini娘,双子",
          "叫这些名字就算点名"),
    field("QQ_AUTO_ACCEPT_INVITE", "自动同意入群邀请", "bool", "groups", "1", ""),
    field("QQ_JOIN_GREET", "进群打招呼", "bool", "groups", "1",
          "刚进群且没人说话时主动冒一句"),
    # 话痨与开口门槛
    field("QQ_CHATTY", "话痨模式", "bool", "chatty", "1",
          "放宽观察评估，激活度过线就接话"),
    field("QQ_CHATTY_HINT", "话痨风格提示", "textarea", "chatty", "",
          "追加到她的人设里的说话风格；留空恢复原本的懒散人设", maxlen=500),
    field("QQ_OBSERVE_ENABLED", "启用群聊观察", "bool", "chatty", "1",
          "关掉后她只在被点名时才说话"),
    field("QQ_OBSERVE_THRESHOLD", "基础开口阈值", "int", "chatty", 6,
          "最终阈值 = 基础阈值 ÷ 该群活跃档位；数值越低越爱说", min=1, max=20),
    field("QQ_THRESHOLD_MIN", "阈值下限", "int", "chatty", 2,
          "再怎么调也不会低于这个值", min=1, max=9),
    field("QQ_THRESHOLD_DYNAMIC", "阈值随群活跃度浮动", "bool", "chatty", "0",
          "开启后群里太热闹会自动把阈值 +1，太冷清 -1"),
    field("QQ_OBSERVE_MIN_MESSAGES", "触发评估的最少消息数", "int", "chatty", 2,
          "攒够几条新消息才评估一次", min=1, max=30),
    field("QQ_OBSERVE_MIN_SPEAKERS", "触发评估的最少人数", "int", "chatty", 1,
          "设 1 表示一个人自言自语她也会接", min=1, max=5),
    field("QQ_OBSERVE_INTERVAL", "评估间隔（秒）", "int", "chatty", 15,
          "两次评估之间的最短间隔", min=5, max=600),
    field("QQ_OBSERVE_COOLDOWN", "发言后长冷却（秒）", "int", "chatty", 45,
          "她主动说完一句后多久不再主动插话", min=5, max=1800),
    field("QQ_COOLDOWN", "普通插话冷却（秒）", "int", "chatty", 10,
          "被点名回复后的最短间隔", min=0, max=600),
    field("QQ_INTERJECT", "随机插话概率", "float", "chatty", 0.02,
          "0 = 从不随机插话，1 = 每条都插", min=0, max=1),
    # 回复形态
    field("QQ_SPLIT_PARTS", "最多拆成几条", "int", "shape", 3,
          "一条回复最多发几条短消息", min=1, max=5),
    field("QQ_TYPING_MS", "打字速度（毫秒/字）", "int", "shape", 90,
          "多条消息之间按字数停顿，越小发得越快", min=20, max=400),
    field("QQ_TYPING_MAX", "单次停顿上限（秒）", "float", "shape", 6,
          "一条特别长的话最多停多久", min=0.5, max=30),
    field("QQ_STICKER_RATE", "发表情概率", "float", "shape", 0.08, "", min=0, max=1),
    field("QQ_NOTICE_RATE", "接群通知概率", "float", "shape", 0.5, "", min=0, max=1),
    field("QQ_GROUP_MAX_TOKENS", "群聊单次输出上限", "int", "shape", 260,
          "调大容易变啰嗦，调小会更短", min=60, max=1024),
    # 额度
    field("QQ_DAILY_CALLS", "群聊每日调用上限", "int", "budget", 600, "", min=0, max=10000),
    field("QQ_OBSERVE_DAILY_CALLS", "观察每日调用上限", "int", "budget", 800,
          "话痨模式主要消耗这一项", min=0, max=10000),
    field("SURF_ENABLED", "允许她自己上网", "bool", "budget", "1", ""),
    field("SURF_DAILY_CALLS", "上网每日次数", "int", "budget", 8, "", min=0, max=200),
    field("SURF_INTERVAL_MINUTES", "上网间隔（分钟）", "int", "budget", 180,
          "", min=10, max=1440),
    # 模型
    field("QQ_GROUP_MODEL", "群聊模型", "text", "model", "gemini-3.5-flash-lite",
          "", maxlen=120, pattern=r"[A-Za-z0-9._\-/]+"),
    field("QQ_OBSERVE_MODEL", "观察评估模型", "text", "model", "gemini-3.5-flash-lite",
          "", maxlen=120, pattern=r"[A-Za-z0-9._\-/]+"),
    field("THINKING_LEVEL", "思考强度", "text", "model", "low",
          "一般填 low / medium / high；留空用模型默认", maxlen=40,
          pattern=r"[A-Za-z0-9_\-]+"),
    field("MAX_OUTPUT_TOKENS", "核心最大输出 token", "int", "model", 512,
          "", min=128, max=4096),
    # 视觉与上下文
    field("QQ_VISION", "允许看图 / 视频", "bool", "misc", "1", ""),
    field("QQ_VISION_MAX_IMAGES", "单次最多看几张图", "int", "misc", 3, "", min=1, max=10),
    field("QQ_VISION_VIDEO_MB", "视频大小上限（MB）", "int", "misc", 20, "", min=1, max=100),
    field("QQ_VISION_VIDEO_SECONDS", "视频时长上限（秒）", "int", "misc", 45, "", min=5, max=300),
    field("QQ_CONTEXT_PROBE", "上下文追问判定", "bool", "misc", "1",
          "判断一句话是不是在接她刚才的话题"),
    field("QQ_BACKFILL_COUNT", "进群补历史条数", "int", "misc", 200, "", min=0, max=500),
    field("QQ_BUFFER_MAX", "群消息缓冲上限", "int", "misc", 240, "", min=20, max=1000),
    field("QQ_GROUP_WARMUP", "进群预热（秒）", "int", "misc", 300,
          "刚进群先看一会儿再说话", min=0, max=1800),
]
CONFIG_KEYS = {item["key"] for item in CONFIG_FIELDS}
FIELDS_BY_KEY = {item["key"]: item for item in CONFIG_FIELDS}


def normalize_value(spec, value):
    """把网页提交的值校验并归一化成能安全写进 .env 的单行字符串。"""
    raw = "" if value is None else str(value).strip()
    kind = spec["kind"]
    if not raw:
        if kind in ("bool",):
            return "0"
        if kind in ("int", "float"):
            return spec["default"]
        if kind == "select":
            return spec["default"]
        return ""
    if kind == "bool":
        low = raw.lower()
        if low in BOOL_ON:
            return "1"
        if low in BOOL_OFF:
            return "0"
        raise ValueError("只能填 0 或 1")
    if kind == "int":
        try:
            number = int(float(raw))
        except (TypeError, ValueError):
            raise ValueError("要填整数")
        low, high = spec.get("min", 0), spec.get("max", 999999)
        if not low <= number <= high:
            raise ValueError(f"范围 {low}~{high}")
        return str(number)
    if kind == "float":
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise ValueError("要填数字")
        low, high = spec.get("min", 0), spec.get("max", 999999)
        if not low <= number <= high:
            raise ValueError(f"范围 {low}~{high}")
        return "%g" % number
    if kind == "list":
        parts = [part for part in re.split(r"[,，\s]+", raw) if part]
        for part in parts:
            if not re.fullmatch(r"[0-9A-Za-z_\-\u4e00-\u9fff.*]+", part):
                raise ValueError("只能填群号 / 昵称 / 数字，不能含特殊符号")
        return ",".join(parts)
    if kind in ("text", "textarea", "select"):
        clean = re.sub(r"[\r\n\t]+", " ", raw)
        limit = spec.get("maxlen", 120)
        if len(clean) > limit:
            raise ValueError(f"最多 {limit} 个字")
        pattern = spec.get("pattern")
        if pattern and clean and not re.fullmatch(pattern, clean):
            raise ValueError("格式不对，只能填字母数字和 . _ - /")
        if kind == "select" and clean not in spec.get("options", []):
            raise ValueError("不在可选值里")
        return clean
    raise ValueError("未知字段类型")


def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def update_env(updates):
    """按 key 原地替换 .env 里的行，保留注释、未知项和原有的顺序。"""
    current = load_env(ENV_PATH)
    changed = [key for key, value in updates.items() if current.get(key, "") != value]
    if not changed:
        return []
    try:
        text = open(ENV_PATH, encoding="utf-8").read()
    except OSError:
        text = ""
    lines = text.splitlines()
    seen = set()
    out = []
    for line in lines:
        matched = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if matched and matched.group(1) in updates:
            key = matched.group(1)
            if key in seen:
                continue
            seen.add(key)
            out.append(f"{key}={updates[key]}")
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    new_text = "\n".join(out).rstrip("\n") + "\n"
    if os.path.exists(ENV_PATH):
        shutil.copy2(ENV_PATH, ENV_BACKUP)
        os.chmod(ENV_BACKUP, 0o600)
    _atomic_write(ENV_PATH, new_text)
    return changed


def read_talk_values():
    try:
        with open(TALK_VALUE_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        return {str(k): float(v) for k, v in data.items()}
    except (OSError, ValueError, TypeError):
        return {}


def write_talk_values(values):
    if not isinstance(values, dict):
        return []
    data = read_talk_values()
    changed = []
    for gid, value in values.items():
        gid = str(gid)
        if not re.fullmatch(r"\d{5,12}", gid):
            continue
        try:
            number = max(0.2, min(3.0, float(value)))
        except (TypeError, ValueError):
            continue
        if abs(float(data.get(gid, 1.0)) - number) > 1e-6:
            data[gid] = number
            changed.append(gid)
    if changed:
        _atomic_write(TALK_VALUE_PATH, json.dumps(data, ensure_ascii=False))
    return changed


def _post_json(url, payload, timeout=6):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def service_urls():
    core = ENV.get("PORT", "8766")
    bridge = ENV.get("QQ_BRIDGE_PORT", "8767")
    return {
        "core": f"http://127.0.0.1:{core}/api/reload",
        "bridge": f"http://127.0.0.1:{bridge}/api/reload",
    }


def reload_services():
    result = {}
    for name, url in service_urls().items():
        try:
            result[name] = {"ok": True, "detail": _post_json(url, {})}
        except Exception as exc:  # noqa: BLE001
            result[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return result


def restart_services():
    try:
        done = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", "gemini-fish", "gemini-fish-qq"],
            capture_output=True, timeout=40, check=False,
        )
        output = (done.stdout + done.stderr).decode("utf-8", "replace").strip()
        return {"ok": done.returncode == 0, "output": output[:400]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "output": f"{type(exc).__name__}: {exc}"}


def bridge_health():
    port = ENV.get("QQ_BRIDGE_PORT", "8767")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=4) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {}


def config_snapshot():
    raw = load_env(ENV_PATH)
    values = {spec["key"]: raw.get(spec["key"], spec["default"]) for spec in CONFIG_FIELDS}
    health = bridge_health()
    groups = health.get("groups") or []
    if not groups:
        groups = [part for part in re.split(r"[,，\s]+", values["QQ_GROUP_WHITELIST"]) if part and part != "*"]
    return {
        "ok": True,
        "groups": CONFIG_GROUPS,
        "fields": CONFIG_FIELDS,
        "values": values,
        "talk_values": read_talk_values(),
        "health": {
            "groups": groups,
            "threshold": health.get("threshold", {}),
            "talk_value": health.get("talk_value", {}),
            "dry": health.get("dry", None),
        },
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_config(body):
    if not isinstance(body, dict):
        return {"ok": False, "error": "请求格式不对"}
    incoming = body.get("values") or {}
    if not isinstance(incoming, dict):
        return {"ok": False, "error": "values 格式不对"}
    updates, errors = {}, []
    for key, value in incoming.items():
        spec = FIELDS_BY_KEY.get(key)
        if not spec:
            continue  # 未知 key 直接忽略，绝不写进 .env
        try:
            updates[key] = normalize_value(spec, value)
        except ValueError as exc:
            errors.append(f"{spec['label']}（{key}）：{exc}")
    if errors:
        return {"ok": False, "error": "；".join(errors[:6])}
    changed = update_env(updates)
    talk_changed = write_talk_values(body.get("talk_values") or {})
    reloaded = reload_services()
    return {
        "ok": True,
        "changed": changed,
        "talk_changed": talk_changed,
        "reloaded": reloaded,
        "reload_ok": all(item.get("ok") for item in reloaded.values()),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gemini 娘 · 后台日志</title>
<style>
:root {
  --bg: #12141a; --panel: #171a22; --line: #242835; --text: #d7dbe5;
  --dim: #7f8798; --accent: #8b7cf6; --ok: #57c88a; --warn: #e6b450; --err: #ef6a6a;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
}
header {
  position: sticky; top: 0; z-index: 5; background: rgba(18,20,26,.96);
  border-bottom: 1px solid var(--line); padding: 10px 16px;
  display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
}
h1 { font-size: 15px; margin: 0 12px 0 0; font-weight: 600; letter-spacing: .02em; }
h1 span { color: var(--accent); }
.tabs { display: flex; flex-wrap: wrap; gap: 6px; }
.tab {
  border: 1px solid var(--line); background: var(--panel); color: var(--dim);
  padding: 5px 10px; border-radius: 6px; cursor: pointer; font-size: 13px;
}
.tab:hover { color: var(--text); border-color: #3a4054; }
.tab.on { color: #fff; border-color: var(--accent); background: #221f3a; }
.controls { margin-left: auto; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
input[type=text], select {
  background: var(--panel); color: var(--text); border: 1px solid var(--line);
  border-radius: 6px; padding: 5px 8px; font-size: 13px;
}
input[type=text] { width: 180px; }
button {
  background: var(--panel); color: var(--text); border: 1px solid var(--line);
  border-radius: 6px; padding: 5px 10px; cursor: pointer; font-size: 13px;
}
button:hover { border-color: var(--accent); }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--dim); }
.dot.live { background: var(--ok); box-shadow: 0 0 8px var(--ok); }
.meta { padding: 6px 16px; color: var(--dim); font-size: 12px; border-bottom: 1px solid var(--line); }
pre {
  margin: 0; padding: 12px 16px 40px; white-space: pre-wrap; word-break: break-word;
  font: 12.5px/1.55 "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
}
pre.nowrap { white-space: pre; word-break: normal; overflow-x: auto; }
.l-err { color: var(--err); }
.l-warn { color: var(--warn); }
.l-ok { color: var(--ok); }
.l-accent { color: #9f92ff; }
.l-dim { color: var(--dim); }
.cfg { max-width: 1000px; padding: 14px 16px 60px; }
.cfg-bar { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.cfg-bar strong { font-size: 15px; }
.cfg-note { color: var(--dim); font-size: 12px; margin-left: 10px; }
.cfg-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.cfg-actions input[type=text] { width: 160px; }
button.primary { background: #2b2550; border-color: var(--accent); color: #fff; }
button.ghost { color: var(--dim); }
.cfg-msg { padding: 8px 12px; border-radius: 6px; margin-bottom: 12px; background: var(--panel); border: 1px solid var(--line); font-size: 12.5px; color: var(--dim); }
.cfg-msg.ok { color: var(--ok); border-color: #2c5540; }
.cfg-msg.err { color: var(--err); border-color: #5a3030; }
details.grp { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; margin-bottom: 10px; padding: 0 14px; }
details.grp > summary { cursor: pointer; padding: 11px 0; font-weight: 600; font-size: 13.5px; list-style: none; }
details.grp > summary::-webkit-details-marker { display: none; }
details.grp > summary::before { content: '\25B8'; color: var(--accent); margin-right: 8px; }
details.grp[open] > summary::before { content: '\25BE'; }
.grp-hint { color: var(--dim); font-weight: 400; font-size: 12px; margin-left: 10px; }
.grp-body { padding: 2px 0 12px; display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px 18px; }
.cfg-row { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.cfg-row label { font-size: 13px; display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.cfg-row .k { color: var(--dim); font-size: 10.5px; font-family: "SF Mono", Menlo, Consolas, monospace; }
.cfg-row .help, .talk-row .help { color: var(--dim); font-size: 11.5px; line-height: 1.45; }
.cfg-row input, .cfg-row select, .cfg-row textarea {
  width: 100%; background: var(--bg); color: var(--text); border: 1px solid var(--line);
  border-radius: 6px; padding: 6px 8px; font-size: 13px;
}
.cfg-row input:focus, .cfg-row select:focus, .cfg-row textarea:focus { outline: none; border-color: var(--accent); }
.cfg-row textarea { resize: vertical; min-height: 62px; }
.talk-row { display: flex; align-items: center; gap: 10px; padding: 6px 0; border-bottom: 1px dashed var(--line); flex-wrap: wrap; }
.talk-row .gid { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 12.5px; min-width: 118px; }
.talk-row input { width: 88px; background: var(--bg); color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 5px 7px; font-size: 12.5px; }
.badge { background: #221f3a; color: #b9aeff; border: 1px solid #3a3358; border-radius: 5px; padding: 2px 7px; font-size: 11.5px; white-space: nowrap; }
.cfg-foot { color: var(--dim); font-size: 12px; margin-top: 16px; line-height: 1.75; }
.cfg-foot code { color: #b9aeff; }
pre[hidden], .controls[hidden], .cfg[hidden], .cfg-row[hidden] { display: none !important; }
.login { max-width: 380px; margin: 18vh auto; padding: 24px; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }
.login h2 { margin: 0 0 12px; font-size: 16px; }
.login p { color: var(--dim); font-size: 12.5px; }
.login input, .login button { width: 100%; margin-top: 10px; }
</style>
</head>
<body>
<header>
  <h1>Gemini 娘 <span>后台日志</span></h1>
  <div class="tabs" id="tabs"></div>
  <div class="controls" id="controls">
    <span class="dot" id="dot"></span>
    <input type="text" id="grep" placeholder="过滤关键词，如 入站 / 回填 / 错误">
    <select id="lines">
      <option value="200">200 行</option>
      <option value="500" selected>500 行</option>
      <option value="1000">1000 行</option>
      <option value="3000">3000 行</option>
    </select>
    <select id="every">
      <option value="0">不自动刷新</option>
      <option value="3000" selected>3 秒</option>
      <option value="10000">10 秒</option>
    </select>
    <button id="wrap">折行</button>
    <button id="order">倒序</button>
    <button id="copy">复制</button>
  </div>
</header>
<div class="meta" id="meta">正在加载…</div>
<pre id="log"></pre>
<div id="config" class="cfg" hidden>
  <div class="cfg-bar">
    <div><strong>运行配置</strong><span class="cfg-note">保存后热重载到核心与桥，立即生效，不清空聊天上下文</span></div>
    <div class="cfg-actions">
      <input type="text" id="cfg-search" placeholder="搜索配置项">
      <button id="cfg-refresh">重新读取</button>
      <button id="cfg-restart" class="ghost">强制重启</button>
      <button id="cfg-save" class="primary">保存并生效</button>
    </div>
  </div>
  <div id="cfg-msg" class="cfg-msg">正在读取配置…</div>
  <div id="cfg-groups"></div>
  <div class="cfg-foot">
    改动直接写到服务器 <code>.env</code>（保存前自动备份为 <code>.env.bak</code>），
    再调用核心与桥的 <code>/api/reload</code> 热重载；API Key、访问令牌、管理员 QQ 不会出现在页面上，也不会被网页改写。<br>
    「强制重启」只在热重载失败或改了需要重启的参数时使用，会清空她正在观察的群聊上下文。
  </div>
</div>
<script>
const state = { src: 'bridge', wrap: true, timer: null, stick: true, at: 0, desc: true };
try {
  state.desc = localStorage.getItem('lv_order') !== 'asc';
} catch (e) { /* 隐私模式下忽略 */ }
const logEl = document.getElementById('log');
const metaEl = document.getElementById('meta');
const dotEl = document.getElementById('dot');

function colorize(line) {
  let cls = '';
  if (/Traceback|Error|ERROR|失败|异常|CRITICAL/.test(line)) cls = 'l-err';
  else if (/\\[跳过\\]|跳过|冷却中/.test(line)) cls = 'l-dim';
  else if (/\\[回填|\\[进群|已同意入群/.test(line)) cls = 'l-ok';
  else if (/\\[入站|\\[画像|\\[摘要/.test(line)) cls = 'l-accent';
  else if (/WARNING|警告/.test(line)) cls = 'l-warn';
  return cls;
}

function applyFilters(lines) {
  const needle = document.getElementById('grep').value.trim().toLowerCase();
  if (!needle) return lines;
  return lines.filter((line) => line.toLowerCase().includes(needle));
}

function render(lines, desc = state.desc) {
  const ordered = desc ? lines.slice().reverse() : lines;
  const frag = document.createDocumentFragment();
  for (const line of ordered) {
    const span = document.createElement('span');
    const cls = colorize(line);
    if (cls) span.className = cls;
    span.textContent = line + '\\n';
    frag.appendChild(span);
  }
  logEl.replaceChildren(frag);
  if (state.stick) logEl.scrollTop = desc ? 0 : logEl.scrollHeight;
}

async function load() {
  if (state.src === 'config') return;
  const n = document.getElementById('lines').value;
  try {
    if (state.src === 'status') {
      const resp = await fetch('/api/status', { cache: 'no-store' });
      const data = await resp.json();
      const s = data.status || {};
      const lines = [`时间  ${s.time}`, ''];
      for (const [unit, value] of Object.entries(s.units || {})) {
        lines.push(`服务  ${unit.padEnd(28)} ${value}`);
      }
      lines.push(`容器  ${'napcat'.padEnd(28)} ${s.napcat}`, '');
      lines.push('桥 /health：');
      lines.push(...String(s.bridge_health || '(无响应)').split('\\n'));
      lines.push('', '日志文件：');
      for (const [name, size] of Object.entries(s.log_files || {})) {
        lines.push(`  ${name.padEnd(28)} ${(size / 1024).toFixed(1)} KB`);
      }
      render(lines, false);
      state.at = Date.now() / 1000;
      dotEl.classList.add('live');
      metaEl.textContent = `运行状态 · 更新于 ${new Date().toLocaleTimeString()}`;
      return;
    }
    const resp = await fetch(`/api/log?src=${encodeURIComponent(state.src)}&n=${n}`, { cache: 'no-store' });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    const kept = applyFilters(data.lines || []);
    render(kept);
    state.at = data.at;
    dotEl.classList.add('live');
    metaEl.textContent = `${data.label} · 共 ${data.lines.length} 行，过滤后 ${kept.length} 行 · 更新于 ${new Date(data.at * 1000).toLocaleTimeString()}`;
  } catch (err) {
    dotEl.classList.remove('live');
    metaEl.textContent = '读取失败：' + err.message;
  }
}

function setSource(src, label) {
  state.src = src;
  state.stick = true;
  document.querySelectorAll('.tab').forEach((el) => el.classList.toggle('on', el.dataset.src === src));
  const isConfig = src === 'config';
  logEl.hidden = isConfig;
  document.getElementById('config').hidden = !isConfig;
  document.getElementById('controls').hidden = isConfig;
  if (isConfig) {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
    dotEl.classList.remove('live');
    metaEl.textContent = '运行配置 · 保存后热重载立即生效';
    loadConfig();
  } else {
    restartTimer();
    load();
  }
}

async function initTabs() {
  const resp = await fetch('/api/sources', { cache: 'no-store' });
  const data = await resp.json();
  const box = document.getElementById('tabs');
  for (const item of data.sources) {
    const btn = document.createElement('button');
    btn.className = 'tab';
    btn.dataset.src = item.id;
    btn.textContent = item.label;
    btn.onclick = () => setSource(item.id, item.label);
    box.appendChild(btn);
  }
  const statusBtn = document.createElement('button');
  statusBtn.className = 'tab';
  statusBtn.dataset.src = 'status';
  statusBtn.textContent = '运行状态';
  statusBtn.onclick = () => setSource('status', '运行状态');
  box.appendChild(statusBtn);
  const cfgBtn = document.createElement('button');
  cfgBtn.className = 'tab';
  cfgBtn.dataset.src = 'config';
  cfgBtn.textContent = '配置';
  cfgBtn.onclick = () => setSource('config', '配置');
  box.appendChild(cfgBtn);
  setSource('bridge');
}

function restartTimer() {
  if (state.timer) clearInterval(state.timer);
  if (state.src === 'config') { state.timer = null; return; }
  const ms = Number(document.getElementById('every').value);
  state.timer = ms ? setInterval(load, ms) : null;
}

logEl.addEventListener('scroll', () => {
  if (state.desc) {
    state.stick = logEl.scrollTop < 60;
  } else {
    state.stick = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 60;
  }
});
document.getElementById('grep').addEventListener('input', load);
document.getElementById('lines').addEventListener('change', load);
document.getElementById('every').addEventListener('change', restartTimer);
document.getElementById('wrap').onclick = () => {
  state.wrap = !state.wrap;
  logEl.classList.toggle('nowrap', !state.wrap);
};
document.getElementById('order').onclick = () => {
  state.desc = !state.desc;
  state.stick = true;
  document.getElementById('order').textContent = state.desc ? '倒序' : '正序';
  try { localStorage.setItem('lv_order', state.desc ? 'desc' : 'asc'); } catch (e) { /* 忽略 */ }
  load();
};
document.getElementById('order').textContent = state.desc ? '倒序' : '正序';
document.getElementById('copy').onclick = async () => {
  await navigator.clipboard.writeText(logEl.innerText);
  metaEl.textContent = '已复制当前视图到剪贴板';
};
// ------------------------------ 配置页 ------------------------------
const cfgMsg = document.getElementById('cfg-msg');
let cfgDirty = false;

function cfgSay(text, kind) {
  cfgMsg.className = 'cfg-msg' + (kind ? ' ' + kind : '');
  cfgMsg.textContent = text;
}

function esc(text) {
  const div = document.createElement('div');
  div.textContent = String(text);
  return div.innerHTML;
}

async function loadConfig() {
  cfgSay('正在读取配置…', '');
  try {
    const resp = await fetch('/api/config', { cache: 'no-store' });
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || '读取失败');
    renderConfig(data);
    cfgDirty = false;
    cfgSay('已读取 · ' + data.at + ' · 修改后点「保存并生效」', 'ok');
  } catch (err) {
    cfgSay('读取失败：' + err.message, 'err');
  }
}

function renderConfig(data) {
  const tv = data.talk_values || {};
  const th = (data.health && data.health.threshold) || {};
  const groups = (data.health && data.health.groups) || [];
  const box = document.getElementById('cfg-groups');
  box.replaceChildren();

  const talk = document.createElement('details');
  talk.className = 'grp';
  talk.open = true;
  const talkSum = document.createElement('summary');
  talkSum.textContent = '各群活跃档位';
  const talkHint = document.createElement('span');
  talkHint.className = 'grp-hint';
  talkHint.textContent = '数值越大越话痨，最终阈值 = 基础阈值 ÷ 档位';
  talkSum.appendChild(talkHint);
  talk.appendChild(talkSum);
  const talkBody = document.createElement('div');
  talkBody.className = 'grp-body';
  talkBody.id = 'cfg-talk';
  if (!groups.length) {
    const empty = document.createElement('div');
    empty.className = 'help';
    empty.textContent = '还没有群。把她拉进群后会自动出现，或在「群白名单」里手动填群号。';
    talkBody.appendChild(empty);
  }
  for (const gid of groups) {
    const row = document.createElement('div');
    row.className = 'talk-row';
    const value = tv[gid] !== undefined ? Number(tv[gid]) : 1.0;
    row.innerHTML = '<span class="gid">' + esc(gid) + '</span>'
      + '<span class="help">档位</span>'
      + '<input type="number" min="0.2" max="3" step="0.1" data-gid="' + esc(gid) + '" value="' + value + '" data-init="' + value + '">'
      + '<span class="badge">当前阈值 ' + (th[gid] !== undefined ? th[gid] : '?') + '</span>'
      + '<span class="help">3.0 话痨(2) · 2.0 活跃(3) · 1.0 普通(6) · 0.7 安静(9)</span>';
    talkBody.appendChild(row);
  }
  talk.appendChild(talkBody);
  box.appendChild(talk);

  for (const grp of data.groups) {
    const details = document.createElement('details');
    details.className = 'grp';
    details.open = !!grp.open;
    const sum = document.createElement('summary');
    sum.textContent = grp.label;
    const hint = document.createElement('span');
    hint.className = 'grp-hint';
    hint.textContent = grp.hint || '';
    sum.appendChild(hint);
    details.appendChild(sum);
    const body = document.createElement('div');
    body.className = 'grp-body';
    for (const spec of data.fields.filter((f) => f.group === grp.id)) {
      const row = document.createElement('div');
      row.className = 'cfg-row';
      row.dataset.key = spec.key;
      const label = document.createElement('label');
      label.textContent = spec.label;
      const code = document.createElement('span');
      code.className = 'k';
      code.textContent = spec.key;
      label.appendChild(code);
      row.appendChild(label);
      let input;
      if (spec.kind === 'bool') {
        input = document.createElement('select');
        input.innerHTML = '<option value="1">开启</option><option value="0">关闭</option>';
        input.value = String(data.values[spec.key]) === '0' ? '0' : '1';
      } else if (spec.kind === 'textarea') {
        input = document.createElement('textarea');
        input.value = data.values[spec.key] || '';
      } else {
        input = document.createElement('input');
        if (spec.kind === 'int' || spec.kind === 'float') {
          input.type = 'number';
          input.step = spec.kind === 'float' ? '0.01' : '1';
          if (spec.min !== undefined) input.min = spec.min;
          if (spec.max !== undefined) input.max = spec.max;
        } else {
          input.type = 'text';
        }
        input.value = data.values[spec.key] || '';
      }
      input.dataset.key = spec.key;
      input.dataset.init = input.value;
      row.appendChild(input);
      if (spec.help) {
        const help = document.createElement('div');
        help.className = 'help';
        help.textContent = spec.help;
        row.appendChild(help);
      }
      body.appendChild(row);
    }
    details.appendChild(body);
    box.appendChild(details);
  }

  box.querySelectorAll('input, select, textarea').forEach((el) => {
    const mark = () => { cfgDirty = true; cfgSay('有未保存的修改，点「保存并生效」', ''); };
    el.addEventListener('input', mark);
    el.addEventListener('change', mark);
  });
}

async function saveConfig() {
  const values = {};
  document.querySelectorAll('#cfg-groups [data-key]').forEach((el) => {
    if (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA') {
      if (el.dataset.init === undefined || el.value !== el.dataset.init) {
        values[el.dataset.key] = el.value;
      }
    }
  });
  const talk_values = {};
  document.querySelectorAll('#cfg-talk [data-gid]').forEach((el) => {
    if (el.dataset.init === undefined || el.value !== el.dataset.init) {
      talk_values[el.dataset.gid] = el.value;
    }
  });
  if (!Object.keys(values).length && !Object.keys(talk_values).length) {
    cfgSay('没有任何改动，无需保存', 'ok');
    return;
  }
  cfgSay('正在保存并热重载…', '');
  try {
    const resp = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ values, talk_values }),
    });
    const data = await resp.json();
    if (!data.ok) { cfgSay('保存失败：' + (data.error || '未知错误'), 'err'); return; }
    const st = Object.entries(data.reloaded || {})
      .map((entry) => entry[0] + (entry[1].ok ? ' 已重载' : ' 重载失败')).join(' · ');
    const summary = data.at + ' · 改了 ' + data.changed.length + ' 项、'
      + data.talk_changed.length + ' 个群档位 · ' + st;
    cfgDirty = false;
    await loadConfig();
    cfgSay(summary, data.reload_ok ? 'ok' : 'err');
  } catch (err) {
    cfgSay('保存失败：' + err.message, 'err');
  }
}

document.getElementById('cfg-save').onclick = saveConfig;
document.getElementById('cfg-refresh').onclick = () => {
  if (cfgDirty && !confirm('有未保存的修改，确定放弃吗？')) return;
  loadConfig();
};
document.getElementById('cfg-restart').onclick = async () => {
  if (!confirm('强制重启会清空她正在观察的群聊上下文，确定吗？')) return;
  cfgSay('正在重启服务…', '');
  try {
    const resp = await fetch('/api/restart', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    const data = await resp.json();
    cfgSay(data.ok ? '已重启 gemini-fish 与 gemini-fish-qq'
      : ('重启失败：' + (data.output || '')), data.ok ? 'ok' : 'err');
    setTimeout(loadConfig, 1800);
  } catch (err) {
    cfgSay('重启失败：' + err.message, 'err');
  }
};
document.getElementById('cfg-search').addEventListener('input', (event) => {
  const needle = event.target.value.trim().toLowerCase();
  document.querySelectorAll('#cfg-groups .cfg-row').forEach((row) => {
    row.hidden = !!needle && !row.textContent.toLowerCase().includes(needle);
  });
  document.querySelectorAll('#cfg-groups details.grp').forEach((details) => {
    if (needle) details.open = true;
  });
});

initTabs();
restartTimer();
</script>
</body>
</html>
"""

LOGIN = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gemini 娘 · 后台日志</title>
<style>
body{margin:0;background:#12141a;color:#d7dbe5;font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.login{max-width:380px;margin:18vh auto;padding:24px;background:#171a22;border:1px solid #242835;border-radius:10px}
h2{margin:0 0 12px;font-size:16px}p{color:#7f8798;font-size:12.5px}
input,button{width:100%;margin-top:10px;padding:8px 10px;background:#12141a;color:#d7dbe5;border:1px solid #242835;border-radius:6px}
button{cursor:pointer}button:hover{border-color:#8b7cf6}
</style></head><body>
<form class="login" method="get" action="/">
  <h2>后台日志</h2>
  <p>需要访问令牌。在服务器 .env 里看 <code>LOGVIEW_TOKEN</code>，或者带 <code>?token=</code> 打开一次即可记住。</p>
  <input type="password" name="token" placeholder="访问令牌" autofocus>
  <button type="submit">进入</button>
</form>
</body></html>
"""


def auth_ok(value: str) -> bool:
    return bool(TOKEN) and hmac.compare_digest(str(value), TOKEN)


class Handler(BaseHTTPRequestHandler):
    server_version = "fish-logview/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def _send(self, body: bytes, status: int = 200, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cookie(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                if key.strip() == "lv":
                    return value.strip()
        return ""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/healthz":
            self._json({"ok": True})
            return

        if parsed.path == "/":
            token = (query.get("token") or [""])[0]
            if auth_ok(token):
                cookie = f"lv={TOKEN}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
                self._send(PAGE.encode("utf-8"), headers={"Set-Cookie": cookie})
                return
            if auth_ok(self._cookie()):
                self._send(PAGE.encode("utf-8"))
                return
            self._send(LOGIN.encode("utf-8"), status=401)
            return

        if not auth_ok(self._cookie()) and not auth_ok((query.get("token") or [""])[0]):
            self._json({"ok": False, "error": "unauthorized"}, 401)
            return

        if parsed.path == "/api/config":
            self._json(config_snapshot())
            return

        if parsed.path == "/api/sources":
            self._json({
                "ok": True,
                "sources": [{"id": key, "label": value[0]} for key, value in SOURCES.items()],
            })
            return

        if parsed.path == "/api/status":
            self._json({"ok": True, "status": status_snapshot()})
            return

        if parsed.path == "/api/log":
            source = (query.get("src") or ["bridge"])[0]
            try:
                want = max(20, min(5000, int((query.get("n") or ["500"])[0])))
            except ValueError:
                want = 500
            entry = SOURCES.get(source)
            if not entry:
                self._json({"ok": False, "error": "unknown source"}, 400)
                return
            lines = entry[1](want)
            self._json({
                "ok": True,
                "source": source,
                "label": entry[0],
                "lines": lines,
                "at": time.time(),
            })
            return

        self._json({"ok": False, "error": "not found"}, 404)


    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not (auth_ok(self._cookie()) or auth_ok((query.get("token") or [""])[0])):
            self._json({"ok": False, "error": "unauthorized"}, 401)
            return
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            self._json({"ok": False, "error": "需要 application/json"}, 400)
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 200000:
            self._json({"ok": False, "error": "请求体为空或过大"}, 400)
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json({"ok": False, "error": "JSON 格式错误"}, 400)
            return
        if not isinstance(body, dict):
            self._json({"ok": False, "error": "JSON 顶层必须是对象"}, 400)
            return
        if parsed.path == "/api/config":
            try:
                self._json(save_config(body))
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        if parsed.path == "/api/restart":
            self._json(restart_services())
            return
        self._json({"ok": False, "error": "not found"}, 404)


def main() -> None:
    if not TOKEN:
        raise SystemExit("缺少 LOGVIEW_TOKEN，先在 .env 里设置一个随机串")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"日志页已启动：http://{HOST}:{PORT}/?token={TOKEN}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
