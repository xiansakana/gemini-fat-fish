#!/usr/bin/env python3
"""验证 API Key 和默认模型是否可用；不会打印密钥。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from server import CFG


def main() -> None:
    key = CFG["GEMINI_API_KEY"]
    if not key:
        print("没有找到 API Key，请先在 .env 里填写 GEMINI_API_KEY。")
        return

    version = CFG["GEMINI_API_VERSION"]
    model = CFG["GEMINI_MODEL"]
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}

    list_url = f"https://generativelanguage.googleapis.com/{version}/models?pageSize=100"
    try:
        with urllib.request.urlopen(urllib.request.Request(list_url, headers=headers), timeout=30) as res:
            data = json.load(res)
        ids = [item.get("name", "").replace("models/", "") for item in data.get("models", [])]
        print("可用模型（Flash 系列节选）：")
        print("\n".join("  - " + i for i in ids if "flash" in i)[:1200])
        print("默认模型在列表中：", model in ids)
    except Exception as exc:  # noqa: BLE001
        print("列出模型失败：", type(exc).__name__, exc)

    payload = {
        "contents": [{"role": "user", "parts": [{"text": "只回复两个字：收到"}]}],
        "generationConfig": {"maxOutputTokens": 16},
    }
    url = f"https://generativelanguage.googleapis.com/{version}/models/{model}:generateContent"
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as res:
            data = json.load(res)
        parts = [
            part.get("text", "")
            for candidate in data.get("candidates", [])
            for part in candidate.get("content", {}).get("parts", [])
        ]
        print("模型回复：", "".join(parts).strip()[:100])
        print("用量：", json.dumps(data.get("usageMetadata", {}), ensure_ascii=False))
    except urllib.error.HTTPError as exc:
        print("调用失败：HTTP", exc.code, exc.read().decode("utf-8", errors="replace")[:500])
    except Exception as exc:  # noqa: BLE001
        print("调用失败：", type(exc).__name__, exc)


if __name__ == "__main__":
    main()
