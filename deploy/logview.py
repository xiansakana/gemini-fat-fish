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
import subprocess
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
    return data.decode("utf-8", "replace").splitlines()[-want:]


def tail_many(paths: list[str], want: int) -> list[str]:
    lines: list[str] = []
    for path in reversed([item for item in paths if os.path.exists(item)]):
        if len(lines) >= want:
            break
        lines = tail_file(path, want - len(lines)) + lines
    return lines[-want:]


def run(cmd: list[str], timeout: int = 15) -> list[str]:
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    text = b"\n".join(part for part in (done.stdout, done.stderr) if part)
    return text.decode("utf-8", "replace").splitlines()


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
  <div class="controls">
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
    <button id="copy">复制</button>
  </div>
</header>
<div class="meta" id="meta">正在加载…</div>
<pre id="log"></pre>
<script>
const state = { src: 'bridge', wrap: true, timer: null, stick: true, at: 0 };
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

function render(lines) {
  const frag = document.createDocumentFragment();
  for (const line of lines) {
    const span = document.createElement('span');
    const cls = colorize(line);
    if (cls) span.className = cls;
    span.textContent = line + '\\n';
    frag.appendChild(span);
  }
  logEl.replaceChildren(frag);
  if (state.stick) logEl.scrollTop = logEl.scrollHeight;
}

async function load() {
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
      render(lines);
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
  load();
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
  setSource('bridge');
}

function restartTimer() {
  if (state.timer) clearInterval(state.timer);
  const ms = Number(document.getElementById('every').value);
  state.timer = ms ? setInterval(load, ms) : null;
}

logEl.addEventListener('scroll', () => {
  const nearBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 60;
  state.stick = nearBottom;
});
document.getElementById('grep').addEventListener('input', load);
document.getElementById('lines').addEventListener('change', load);
document.getElementById('every').addEventListener('change', restartTimer);
document.getElementById('wrap').onclick = () => {
  state.wrap = !state.wrap;
  logEl.classList.toggle('nowrap', !state.wrap);
};
document.getElementById('copy').onclick = async () => {
  await navigator.clipboard.writeText(logEl.innerText);
  metaEl.textContent = '已复制当前视图到剪贴板';
};
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


def main() -> None:
    if not TOKEN:
        raise SystemExit("缺少 LOGVIEW_TOKEN，先在 .env 里设置一个随机串")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"日志页已启动：http://{HOST}:{PORT}/?token={TOKEN}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
