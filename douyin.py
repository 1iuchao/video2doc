#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
抖音专用解析与下载。

为什么不用 yt-dlp：抖音的 aweme/detail 接口要 a_bogus + msToken 签名，
yt-dlp 拿不到，稳定报 403。而浏览器自己会算签名，所以这里用 headless Edge
打开视频页、拦官方接口响应，拿 play_addr 后自行下载。
"""

import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

import paths

NODE_SCRIPT = Path(__file__).resolve().parent / "tools" / "douyin_detail.js"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

ID_RE = re.compile(r"(?:/video/|/share/video/|aweme_id=)(\d{15,25})")


def is_douyin(url):
    return bool(url) and ("douyin.com" in url or "iesdouyin.com" in url)


def resolve_aweme_id(url):
    """短链 -> 作品 id。先看 URL 本身，再跟随跳转，最后从页面里正则找。"""
    m = ID_RE.search(url or "")
    if m:
        return m.group(1)

    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        final = r.geturl()
        m = ID_RE.search(final)
        if m:
            return m.group(1)
        html = r.read().decode("utf-8", "replace")
    m = ID_RE.search(html)
    return m.group(1) if m else None


def fetch_detail(aweme_id, profile_dir=None, timeout=90):
    """调 node 脚本抓详情。返回 (aweme_dict | None, error | None)。"""
    if not NODE_SCRIPT.exists():
        return None, f"找不到 {NODE_SCRIPT}"
    cmd = ["node", str(NODE_SCRIPT), str(aweme_id)]
    if profile_dir:
        cmd += ["--profile", str(profile_dir)]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None, "找不到 node（需要 Node 22+，装完重开终端）"
    except subprocess.TimeoutExpired:
        return None, f"抓取超时（>{timeout}s）"

    out = (p.stdout or "").strip()
    if not out:
        return None, (p.stderr or "").strip()[:400] or "node 脚本无输出"
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return None, f"node 输出不是 JSON：{out[:300]}"

    if not d.get("ok"):
        return None, d.get("error") or "未知错误"
    return d["aweme"], None


def to_meta(aweme):
    """转成和 yt-dlp 分支统一的 meta 结构。"""
    ts = aweme.get("create_time") or 0
    up_date = time.strftime("%Y%m%d", time.localtime(ts)) if ts else ""
    st = aweme.get("statistics") or {}
    dur = aweme.get("duration_ms") or 0
    return {
        "id": aweme.get("id") or "",
        "title": aweme.get("desc") or "",
        "uploader": aweme.get("author") or "",
        "duration": round(dur / 1000, 1) if dur else None,
        "like_count": st.get("digg"),
        "comment_count": st.get("comment"),
        "share_count": st.get("share"),
        "collect_count": st.get("collect"),
        "upload_date": up_date,
        "description": aweme.get("desc") or "",
        "music": aweme.get("music") or "",
        "webpage_url": f"https://www.douyin.com/video/{aweme.get('id')}",
        "create_ts": ts,
    }


def download(play_urls, dest, timeout=180):
    """按顺序试每个播放地址，成功即返回路径。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_err = None

    for i, u in enumerate(play_urls or []):
        try:
            req = urllib.request.Request(u, headers={
                "User-Agent": UA,
                "Referer": "https://www.douyin.com/",
                "Accept": "*/*",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ctype = r.headers.get("Content-Type", "")
                data = r.read()
            if len(data) < 10240:
                last_err = f"url[{i}] 内容过小（{len(data)} 字节）"
                continue
            if "video" not in ctype and data[4:8] != b"ftyp":
                last_err = f"url[{i}] 不是视频（Content-Type={ctype}）"
                continue
            dest.write_bytes(data)
            return dest, None
        except Exception as e:
            last_err = f"url[{i}] {type(e).__name__}: {e}"
            continue

    return None, last_err or "没有可用的播放地址"
