#!/usr/bin/env python3
"""拉取 iptv-org 中国公开直播源，逐条测活，去重分组，生成 iptv-cn.m3u 订阅文件。

数据来源: https://github.com/iptv-org/iptv （公开免费源集合）
仅用 Python 标准库，CI 无需安装依赖。

策略:
  - 探测成功          -> 保留
  - 探测失败 + 无地域限制标记 -> 剔除（大概率已失效）
  - 探测失败 + 带 [Geo-blocked] 标记 -> 保留并归入「需国内网络」分组
    （Actions 跑在海外，探测失败但国内大概率可看）
"""

import asyncio
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

SRC_URL = os.environ.get(
    "IPTV_SRC_URL", "https://iptv-org.github.io/iptv/countries/cn.m3u")
OUT_PATH = Path(os.environ.get(
    "IPTV_OUT_PATH", str(Path(__file__).resolve().parent.parent / "iptv-cn.m3u")))
CONCURRENCY = 30
PROBE_LIMIT = int(os.environ.get("PROBE_LIMIT", "0") or 0)  # 仅本地冒烟测试用
TIMEOUT = 12
UA = "VLC/3.0.21 LibVLC/3.0.21"
BEIJING = timezone(timedelta(hours=8))
ATTR_RE = re.compile(r'([A-Za-z0-9-]+)="([^"]*)"')
NUM_RE = re.compile(r"\d+")

GROUP_RANK = {"央视": 0, "卫视": 1, "地方及其他": 2, "需国内网络": 3}


def parse_playlist(text):
    channels, pending = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attrs = dict(ATTR_RE.findall(line))
            name = line.split(",", 1)[1].strip() if "," in line else "Unknown"
            pending = {"name": name, "attrs": attrs, "url": None}
        elif pending is not None and not line.startswith("#"):
            pending["url"] = line
            channels.append(pending)
            pending = None
    return channels


def is_hls(url):
    return urlparse(url).path.lower().endswith((".m3u8", ".m3u"))


def fetch(url, read_size=65536):
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=TIMEOUT) as resp:
        if resp.status >= 400:
            raise URLError(f"HTTP {resp.status}")
        return resp.read(read_size)


def probe(ch):
    url = ch["url"]
    try:
        chunk = fetch(url)
        if is_hls(url):
            return b"#EXTM3U" in chunk
        return len(chunk) > 0
    except Exception:
        return False


def group_of(ch):
    ident = (ch["attrs"].get("tvg-id") or "").lower()
    name, low = ch["name"], ch["name"].lower()
    if ident.startswith(("cctv", "cgtn", "cmc")) or "cctv" in low or "cgtn" in low:
        return "央视"
    if "卫视" in name or "satellite tv" in low:
        return "卫视"
    return "地方及其他"


def natural_key(ch):
    nums = [int(n) for n in NUM_RE.findall(ch["name"])]
    return (GROUP_RANK[ch["group"]], nums[0] if nums else 9999, ch["name"].lower(), ch["url"])


def main():
    text = fetch(SRC_URL).decode("utf-8", "ignore")
    channels = parse_playlist(text)
    if PROBE_LIMIT > 0:
        channels = channels[:PROBE_LIMIT]

    # 按 URL 去重
    seen, unique = set(), []
    for ch in channels:
        if ch["url"] in seen:
            continue
        seen.add(ch["url"])
        ch["geo"] = "[Geo-blocked]" in ch["name"]
        unique.append(ch)

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        results = list(pool.map(probe, unique))
    for ch, ok in zip(unique, results):
        ch["alive"] = ok

    kept, dropped, geo_kept = [], 0, 0
    for ch in unique:
        if ch["alive"]:
            ch["group"] = group_of(ch)
            kept.append(ch)
        elif ch["geo"]:
            ch["group"] = "需国内网络"
            geo_kept += 1
            kept.append(ch)
        else:
            dropped += 1

    kept.sort(key=natural_key)

    now = datetime.now(BEIJING)
    lines = [
        "#EXTM3U",
        f"# 更新时间: {now:%Y-%m-%d %H:%M}（北京时间）",
        f"# 数据来源: iptv-org 公开免费源集合（{SRC_URL}）",
        f"# 本次探测: 总计 {len(unique)} 条，保留 {len(kept)} 条"
        f"（其中 {geo_kept} 条海外探测失败但有地域限制标记，已归入「需国内网络」分组），剔除 {dropped} 条",
        "# 频道版权归各播出机构所有；本文件仅聚合公开免费流，失效属正常现象，每日自动更新",
    ]
    for ch in kept:
        ident = ch["attrs"].get("tvg-id", "")
        lines.append(f'#EXTINF:-1 tvg-id="{ident}" group-title="{ch["group"]}",{ch["name"]}')
        lines.append(ch["url"])

    OUT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def count(g):
        return sum(1 for c in kept if c["group"] == g)

    summary = (
        f"总计 {len(unique)} | 保留 {len(kept)}（央视 {count('央视')}，卫视 {count('卫视')}，"
        f"地方 {count('地方及其他')}，需国内网络 {geo_kept}）| 剔除 {dropped}"
    )
    print(summary)
    gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_summary:
        Path(gh_summary).write_text(f"## 直播源更新结果\n\n```\n{summary}\n```\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
