#!/usr/bin/env python3
"""给 Gemini 娘用的联网工具：搜索、读网页、看 B 站。

只读、无登录态、结果都压成短文本交给模型；所有 URL 都做 SSRF 过滤，
避免被群里的人拿来探测内网或云元数据。
"""

from __future__ import annotations

import gzip
import html as html_lib
import ipaddress
import json
import re
import socket
import subprocess
import threading
import urllib.parse
import zlib
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
BROWSER_LOCK = threading.Lock()
MAX_BYTES = 600_000


def _safe_url(url: str) -> bool:
    """拒绝内网 / 回环 / 链路本地 / 元数据地址，防止 SSRF。"""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def _http_get(url: str, timeout: int = 15) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9", "Accept-Encoding": "identity"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_BYTES)


def _decode(raw: bytes) -> str:
    """有的站点无视 identity 仍然压 gzip，还有的是 GBK，这里统一兜住。"""
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    elif raw[:2] == b"\x78\x9c" or raw[:2] == b"\x78\x01":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
    for encoding in ("utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if text.count("\ufffd") < max(1, len(text) // 100):
            return text
    return raw.decode("utf-8", "replace")


def _json_get(url: str, timeout: int = 15) -> dict:
    return json.loads(_decode(_http_get(url, timeout)))


def strip_html(raw: str) -> str:
    raw = re.sub(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html_lib.unescape(raw)
    lines = [re.sub(r"[ \t\u3000]+", " ", line).strip() for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def search_web(query: str, limit: int = 5) -> str:
    """Bing 网页搜索，返回标题 / 链接 / 摘要。"""
    query = str(query or "").strip()[:200]
    if not query:
        return "要搜什么？给个关键词。"
    limit = max(1, min(8, int(limit or 5)))
    url = "https://www.bing.com/search?" + urllib.parse.urlencode(
        {"q": query, "setlang": "zh-Hans", "count": limit, "mkt": "zh-CN"}
    )
    if not _safe_url(url):
        return "这个搜索地址不通。"
    items = _search_bing(query, url, limit)
    if not items:
        items = _search_ddg(query, limit)
    if not items:
        return f"没搜到「{query}」的结果。"
    return f"搜索「{query}」的结果：\n" + "\n".join(items)


def _search_bing(query: str, url: str, limit: int) -> list[str]:
    try:
        page = _decode(_http_get(url))
    except Exception:  # noqa: BLE001
        return []
    items: list[str] = []
    for block in re.findall(r'<li class="b_algo".*?</li>', page, flags=re.S)[:limit * 2]:
        link = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, flags=re.S)
        if not link:
            continue
        title = strip_html(link.group(2))[:120]
        href = html_lib.unescape(link.group(1))
        snippet = re.search(r"<p[^>]*>(.*?)</p>", block, flags=re.S)
        summary = strip_html(snippet.group(1))[:220].replace("阅读更多", "").strip()
        items.append(f"{len(items) + 1}. {title}\n   {href}\n   {summary}")
        if len(items) >= limit:
            break
    return items


def _search_ddg(query: str, limit: int) -> list[str]:
    """Bing 解析不到时的备用源。"""
    url = "https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": query})
    try:
        page = _decode(_http_get(url))
    except Exception:  # noqa: BLE001
        return []
    titles = re.findall(r"class=.result-link.[^>]*>(.*?)</a>", page, flags=re.S)
    links = re.findall(r"class=.result-link.[^>]*href=\"([^\"]+)\"", page, flags=re.S)
    items: list[str] = []
    for index, title in enumerate(titles[:limit]):
        href = html_lib.unescape(links[index]) if index < len(links) else ""
        items.append(f"{index + 1}. {strip_html(title)[:120]}\n   {href}")
    return items


def render_url(url: str, timeout: int = 40) -> str:
    """用无头 Chromium 渲染 JS 页面，拿渲染后的文本。"""
    if not _safe_url(url):
        return "这个地址不允许访问。"
    command = [
        "chromium", "--headless", "--no-sandbox", "--disable-gpu",
        "--disable-dev-shm-usage", "--hide-scrollbars", "--mute-audio",
        "--user-agent", UA, "--virtual-time-budget=8000", "--dump-dom", url,
    ]
    if not BROWSER_LOCK.acquire(timeout=15):
        return "浏览器正忙，稍后再试。"
    try:
        done = subprocess.run(
            command, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "页面渲染超时。"
    finally:
        BROWSER_LOCK.release()
    dom = done.stdout.decode("utf-8", "replace")
    if not dom:
        return "页面没渲染出内容。"
    return dom


def open_url(url: str, render: bool = False, max_chars: int = 5000) -> str:
    """读网页正文；没有 JS 渲染也有内容就直接用 HTML，否则开浏览器。"""
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "只认 http/https 网址。"
    if not _safe_url(url):
        return "这个地址不允许访问。"
    text = ""
    if not render:
        try:
            raw = _decode(_http_get(url))
            text = strip_html(raw)
        except Exception:  # noqa: BLE001
            text = ""
        garbled = text.count("\ufffd") > max(3, len(text) // 200)
        if len(text) < 400 or garbled:
            render = True
    if render:
        dom = render_url(url)
        if dom.startswith(("这个地址", "页面", "浏览器")):
            return dom
        text = strip_html(dom)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return "这个页面没读到正文。"
    return "来源：" + url + "\n" + text[: max(500, int(max_chars))]


def _bvid(value: str) -> str:
    text = str(value or "")
    match = re.search(r"BV[0-9A-Za-z]{10}", text)
    return match.group(0) if match else ""


def bilibili_video(bvid: str, replies: int = 4) -> str:
    """看一个 B 站视频：基本信息 + 热评，用来接着聊内容。"""
    code = _bvid(bvid)
    if not code:
        return "需要一个 BV 号，比如 BV19k1Jz5Ea8。"
    try:
        data = _json_get(f"https://api.bilibili.com/x/web-interface/view?bvid={code}")
    except Exception as exc:  # noqa: BLE001
        return f"打不开这个视频：{type(exc).__name__}"
    if data.get("code") != 0:
        return "B 站说没这个视频：" + str(data.get("message"))
    info = data.get("data") or {}
    owner = (info.get("owner") or {}).get("name", "")
    stat = info.get("stat") or {}
    lines = [
        f"视频：{info.get('title', '')}",
        f"UP 主：{owner} | 分区：{info.get('tname', '')} | 时长 {int(info.get('duration', 0) or 0) // 60} 分 {int(info.get('duration', 0) or 0) % 60} 秒",
        f"播放 {stat.get('view', 0)} / 弹幕 {stat.get('danmaku', 0)} / 评论 {stat.get('reply', 0)} / 点赞 {stat.get('like', 0)}",
    ]
    desc = str(info.get("desc") or "").strip()
    if desc:
        lines.append("简介：" + re.sub(r"\s+", " ", desc)[:300])
    try:
        hot = _json_get(
            f"https://api.bilibili.com/x/v2/reply?type=1&oid={info.get('aid')}&sort=2&ps={max(1, min(8, replies))}"
        )
        messages = [
            re.sub(r"\s+", " ", str(item.get("content", {}).get("message", "")))[:80]
            for item in ((hot.get("data") or {}).get("replies") or [])
        ]
        messages = [item for item in messages if item]
        if messages:
            lines.append("热评：" + " / ".join(messages))
    except Exception:  # noqa: BLE001
        pass
    lines.append(f"链接：https://www.bilibili.com/video/{code}")
    return "\n".join(lines)


def popular_items(limit: int = 10) -> list[dict]:
    """热门榜原始条目，供“自己刷视频”挑选。"""
    try:
        data = _json_get(
            "https://api.bilibili.com/x/web-interface/popular?"
            + urllib.parse.urlencode({"ps": max(1, min(20, int(limit or 10))), "pn": 1})
        )
    except Exception:  # noqa: BLE001
        return []
    return ((data.get("data") or {}).get("list") or [])


def video_info(bvid: str) -> dict:
    code = _bvid(bvid)
    if not code:
        return {}
    try:
        data = _json_get(f"https://api.bilibili.com/x/web-interface/view?bvid={code}")
    except Exception:  # noqa: BLE001
        return {}
    return data.get("data") or {}


def bilibili_popular(limit: int = 5) -> str:
    """看 B 站当前热门，拿去当“刚刷到”的素材。"""
    limit = max(1, min(10, int(limit or 5)))
    items = popular_items(limit)[:limit]
    if not items:
        return "热门榜暂时拿不到。"
    lines = ["B 站当前热门："]
    for index, item in enumerate(items, start=1):
        lines.append(
            f"{index}. {item.get('title', '')} | UP {((item.get('owner') or {}).get('name', ''))} "
            f"| {item.get('bvid', '')} | 播放 {(item.get('stat') or {}).get('view', 0)}"
        )
    return "\n".join(lines)
