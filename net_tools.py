#!/usr/bin/env python3
"""给 Gemini 娘用的联网工具：搜索、读网页、看 B 站、查行情和财经快讯。

只读、无登录态、结果都压成短文本交给模型；所有 URL 都做 SSRF 过滤，
避免被群里的人拿来探测内网或云元数据。
"""

from __future__ import annotations

import gzip
import html as html_lib
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import zlib
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
BROWSER_LOCK = threading.Lock()
SEARCH_CACHE: dict[str, tuple[float, str]] = {}
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
    """先查 DuckDuckGo（中文结果质量明显更好），拿不到再用 Bing RSS 兜底。"""
    query = str(query or "").strip()[:200]
    if not query:
        return "要搜什么？给个关键词。"
    limit = max(1, min(8, int(limit or 5)))
    cached = SEARCH_CACHE.get(query)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    items = _search_ddg(query, limit)
    if not items:
        items = _search_rss(query, limit)
    if not items:
        return f"没搜到「{query}」的结果，可能被限流了，等会儿再试。"
    result = f"搜索「{query}」的结果：\n" + "\n".join(items)
    SEARCH_CACHE[query] = (time.time(), result)
    if len(SEARCH_CACHE) > 40:
        for key in sorted(SEARCH_CACHE, key=lambda k: SEARCH_CACHE[k][0])[:10]:
            SEARCH_CACHE.pop(key, None)
    return result


def _search_rss(query: str, limit: int) -> list[str]:
    """Bing 的 RSS 结果没有反爬，速度快、排序也正常。"""
    import xml.etree.ElementTree as ET

    url = "https://www.bing.com/search?" + urllib.parse.urlencode(
        {"q": query, "format": "rss", "count": limit}
    )
    try:
        raw = _decode(_http_get(url, timeout=12))
        root = ET.fromstring(raw)
    except Exception:  # noqa: BLE001
        return []
    items: list[str] = []
    for node in root.findall(".//item")[:limit]:
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        desc = strip_html(node.findtext("description") or "")[:200].replace("阅读更多", "").strip()
        if not title:
            continue
        items.append(f"{len(items) + 1}. {title[:120]}\n   {link}\n   {desc}")
    return items


def _search_ddg(query: str, limit: int) -> list[str]:
    """DuckDuckGo HTML 版：中文结果质量比 Bing RSS 好，带标题 + 链接 + 摘要。"""
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    try:
        page = _decode(_http_get(url))
    except Exception:  # noqa: BLE001
        return []
    titles = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, flags=re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, flags=re.S)
    items: list[str] = []
    for index, (href, title) in enumerate(titles[:limit]):
        target = html_lib.unescape(href)
        redirect = re.search(r"[?&]uddg=([^&]+)", target)
        if redirect:
            target = urllib.parse.unquote(redirect.group(1))
        line = f"{index + 1}. {strip_html(title)[:120]}\n   {target}"
        if index < len(snippets):
            desc = strip_html(snippets[index])[:200].strip()
            if desc:
                line += "\n   " + desc
        items.append(line)
    return items


def render_url(url: str, timeout: int = 40) -> str:
    """用无头 Chromium 渲染 JS 页面，拿渲染后的文本。"""
    if not _safe_url(url):
        return "这个地址不允许访问。"
    profile = tempfile.mkdtemp(prefix="fish-chrome-")
    command = [
        "chromium", "--headless", "--no-sandbox", "--disable-gpu",
        "--disable-dev-shm-usage", "--hide-scrollbars", "--mute-audio",
        f"--user-data-dir={profile}",
        f"--user-agent={UA}", "--virtual-time-budget=4000", "--dump-dom", url,
    ]
    if not BROWSER_LOCK.acquire(timeout=15):
        shutil.rmtree(profile, ignore_errors=True)
        return "浏览器正忙，稍后再试。"
    try:
        done = subprocess.run(
            command, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "页面渲染超时。"
    finally:
        BROWSER_LOCK.release()
        shutil.rmtree(profile, ignore_errors=True)
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


# ---------------- 行情与财经快讯 ----------------
# 通用搜索引擎(Bing RSS)搜中文财经只会返回网页链接描述，拿不到实时数字，
# 所以行情和消息面走专用源：腾讯行情 qt.gtimg.cn + 新浪财经 7x24 快讯。

INDEX_CODES = {
    "上证指数": "sh000001", "上证综指": "sh000001", "上证": "sh000001",
    "大盘": "sh000001", "沪指": "sh000001",
    "深证成指": "sz399001", "深成指": "sz399001", "深指": "sz399001",
    "创业板指": "sz399006", "创业板": "sz399006",
    "沪深300": "sh000300", "沪深三百": "sh000300",
    "科创50": "sh000688",
    "中证500": "sh000905",
    "中证1000": "sh000852",
    "恒生指数": "hkHSI", "恒生": "hkHSI",
    "道琼斯": "usDJI", "纳指": "usIXIC", "纳斯达克": "usIXIC",
    "标普500": "usINX", "标普": "usINX",
}


def _decode_u_escapes(text: str) -> str:
    """smartbox 返回的是字面 \\uXXXX 序列，这里手工还原成汉字。"""
    return re.sub(
        r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text
    )


def _tencent_code(name: str) -> str:
    """把名称或代码解析成腾讯行情代码；解析不了返回空串。"""
    text = str(name or "").strip().lower()
    if not text:
        return ""
    if text in INDEX_CODES:
        return INDEX_CODES[text]
    for key, value in INDEX_CODES.items():
        if key in text and len(text) <= len(key) + 2:
            return value
    match = re.fullmatch(r"(sh|sz|hk|us)?(\d{5,6})", text)
    if match:
        prefix, digits = match.group(1) or "", match.group(2)
        if prefix:
            return prefix + digits
        if digits.startswith(("6", "9", "5")):
            return "sh" + digits
        if digits.startswith(("0", "2", "3")):
            return "sz" + digits
    # 名称搜代码：smartbox 返回 sh~600519~贵州茅台~gzmt~GP-A^...^
    url = "https://smartbox.gtimg.cn/s3/?v=2&t=all&" + urllib.parse.urlencode({"q": text})
    try:
        raw = _decode(_http_get(url, timeout=8))
        hint = re.search(r'v_hint="([^"]*)"', raw)
        if not hint:
            return ""
        first = _decode_u_escapes(hint.group(1)).split("^")[0]
        parts = first.split("~")
        if len(parts) >= 2 and parts[0] in ("sh", "sz", "hk", "us", "bj"):
            return parts[0] + parts[1]
    except Exception:  # noqa: BLE001
        pass
    return ""


def _parse_quote(payload: str) -> dict:
    match = re.search(r'v_([a-z]+\w*)="([^"]*)"', payload)
    if not match:
        return {}
    fields = match.group(2).split("~")
    if len(fields) < 35 or not fields[3]:
        return {}

    def pick(index: str) -> str:
        return fields[int(index)] if int(index) < len(fields) else ""

    code = match.group(1)
    if code.startswith("v_"):
        code = code[2:]
    stamp = pick("30")  # 形如 20261127161427
    when = ""
    if len(stamp) == 14:
        when = f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[8:10]}:{stamp[10:12]}"
    return {
        "code": code,
        "name": pick("1"),
        "price": pick("3"),
        "prev_close": pick("4"),
        "open": pick("5"),
        "change": pick("31"),
        "pct": pick("32"),
        "high": pick("33"),
        "low": pick("34"),
        "turnover": pick("38"),
        "amount": pick("37"),
        "pe": pick("39"),
        "market_cap": pick("45"),
        "when": when,
    }


def _format_quote(q: dict) -> str:
    try:
        change = float(q["change"])
        sign = "+" if change >= 0 else ""
    except (KeyError, ValueError):
        sign = ""
    bits = [f"{q['name']}({q['code']}) 最新 {q['price']}"]
    if q["change"] and q["pct"]:
        bits.append(f"{sign}{q['change']}（{sign}{q['pct']}%）")
    for label, key in (("昨收", "prev_close"), ("今开", "open"), ("最高", "high"), ("最低", "low")):
        if q.get(key):
            bits.append(f"{label} {q[key]}")
    # 指数（sh000xxx / sz399xxx）的换手和市值字段是成交额，展示出来会误导
    is_index = bool(re.match(r"^(sh000|sz399|us|hk)", q.get("code", "")))
    if not is_index and q.get("turnover") and q["turnover"] not in ("-", "0.00"):
        bits.append(f"换手 {q['turnover']}%")
    if not is_index and q.get("market_cap") and q["market_cap"] not in ("-", "0.00"):
        try:
            cap = float(q["market_cap"])
            bits.append(f"总市值 {cap / 1e8:.0f} 亿" if cap >= 1e8 else f"总市值 {cap:.0f}")
        except ValueError:
            pass
    if q.get("when"):
        bits.append(f"截至 {q['when']}")
    return "，".join(bits)


def finquote(names: str) -> str:
    """查指数/个股实时行情。names 逗号分隔，可以是中文名或代码。"""
    tokens = [item for item in re.split(r"[,，、\s]+", str(names or "")) if item][:6]
    if not tokens:
        return "要查哪个？给个名称（如 上证指数、贵州茅台）或代码（如 600519）。"
    codes: list[str] = []
    missed: list[str] = []
    for token in tokens:
        code = _tencent_code(token)
        if code:
            codes.append(code)
        else:
            missed.append(token)
    if not codes:
        return "没认出这些代码或名称：" + "、".join(missed)
    try:
        raw = _decode(_http_get("https://qt.gtimg.cn/q=" + ",".join(codes), timeout=8))
    except Exception as exc:  # noqa: BLE001
        return f"行情接口挂了：{type(exc).__name__}"
    lines = []
    for chunk in re.findall(r'v_[a-z]+\w+="[^"]*"', raw):
        quote = _parse_quote(chunk)
        if quote:
            lines.append(_format_quote(quote))
    if not lines:
        return "行情接口没返回数据。"
    for token in missed:
        lines.append(f"「{token}」没认出来，试试代码。")
    return "\n".join(lines)


def finnews(limit: int = 12) -> str:
    """新浪财经 7x24 快讯，拿最近的财经新闻要点。"""
    limit = max(3, min(20, int(limit or 12)))
    url = (
        "https://zhibo.sina.com.cn/api/zhibo/feed?"
        + urllib.parse.urlencode(
            {"page": 1, "page_size": limit, "zhibo_id": 152, "tag_id": 0, "dire": "f", "dpc": 1}
        )
    )
    try:
        data = _json_get(url, timeout=10)
    except Exception as exc:  # noqa: BLE001
        return f"快讯接口挂了：{type(exc).__name__}"
    items = ((data.get("result", {}).get("data", {}) or {}).get("feed", {}) or {}).get("list") or []
    if not items:
        return "快讯接口没返回内容。"
    lines = [f"新浪财经 7x24 快讯（最新 {len(items)} 条）："]
    for item in items:
        text = strip_html(str(item.get("rich_text", "")))[:120]
        when = str(item.get("create_time", ""))[11:16]
        tags = item.get("tag") or []
        tag = f"[{tags[0].get('name')}]" if tags else ""
        lines.append(f"{when} {tag}{text}")
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
