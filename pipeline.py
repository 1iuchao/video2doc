#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
主流程：链接 -> 下载 -> 抽音频 -> 转写 -> 落转录稿。

只做「取到文字」这一段。分析、清洗、命名成稿由调用它的 agent 完成。

用法：
    python pipeline.py "<分享文案或链接>"
    python pipeline.py "<链接>" --lang auto --agent "模型名"
    python pipeline.py --check        环境自检
    python pipeline.py --cleanup      清理超期中间文件

stdout 只有一行 JSON，进度日志走 stderr。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import douyin
import gpu
import paths
from workspace import Workspace, guess_summary, today_str


def log(msg):
    sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    sys.stderr.flush()


def run(cmd, timeout=None):
    try:
        p = subprocess.run([str(c) for c in cmd], capture_output=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", f"命令超时（>{timeout}s）"
    except FileNotFoundError as e:
        return -2, "", f"找不到可执行文件：{e}"


URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#@!$&'()*+,;=%]+")


def extract_url(text):
    urls = URL_RE.findall(text or "")
    if not urls:
        return None
    for u in urls:
        if "douyin" in u or "iesdouyin" in u:
            return u.rstrip(".,;)")
    return urls[0].rstrip(".,;)")


# ---------- 非抖音站点：yt-dlp ----------

def ytdlp_bin(cfg):
    return paths.engine_dir(cfg) / "yt-dlp.exe"


def ytdlp_base(cfg):
    base = [ytdlp_bin(cfg)]
    if cfg.get("proxy"):
        base += ["--proxy", cfg["proxy"]]
    if cfg.get("cookies_file"):
        base += ["--cookies", cfg["cookies_file"]]
    return base


def ytdlp_meta(url, cfg):
    rc, out, err = run(ytdlp_base(cfg) + [
        "--dump-single-json", "--no-playlist", "--no-warnings",
        "--socket-timeout", "30", url], timeout=120)
    if rc != 0 or not out.strip():
        log(f"元数据抓取失败：{err.strip()[:200]}")
        return {}
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return {}
    return {
        "id": str(d.get("id") or ""),
        "title": d.get("title") or "",
        "uploader": d.get("uploader") or "",
        "duration": d.get("duration"),
        "like_count": d.get("like_count"),
        "comment_count": d.get("comment_count"),
        "share_count": d.get("repost_count"),
        "collect_count": None,
        "upload_date": d.get("upload_date") or "",
        "description": (d.get("description") or "")[:2000],
        "webpage_url": d.get("webpage_url") or url,
    }


def ytdlp_download(url, cfg, media_dir):
    media_dir.mkdir(parents=True, exist_ok=True)
    cmd = ytdlp_base(cfg) + [
        "--no-playlist", "--no-warnings", "--socket-timeout", "30",
        "--no-part", "--no-mtime", "-f", "bv*+ba/b",
        "--merge-output-format", "mp4", "-o", "%(id)s.%(ext)s",
        "-P", str(media_dir), "--print", "after_move:filepath", url]
    log("开始下载…")
    rc, out, err = run(cmd, timeout=900)
    if rc != 0:
        return None, (err.strip() or out.strip())[:500]
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line and Path(line).exists():
            return Path(line), None
    files = sorted(media_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return (files[0], None) if files else (None, "下载完成但找不到输出文件")


# ---------- 音频与转写 ----------

def to_wav(video, cfg, work_dir):
    ffmpeg = paths.ffmpeg(cfg)
    if not ffmpeg:
        return None, "找不到 ffmpeg。先跑 python setup.py"
    wav = work_dir / (video.stem + ".wav")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
           "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
    log("抽取音频…")
    rc, _, err = run(cmd, timeout=600)
    if rc != 0 or not wav.exists():
        return None, f"ffmpeg 失败：{err.strip()[:300]}"
    return wav, None


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _run_transcribe(exe, model, wav, device, lang, ws, label):
    """跑一次 vibe-server transcribe。返回 (text | None, error | None)。"""
    cmd = [exe, "transcribe", str(model), str(wav), "--gpu-device", str(device)]
    if lang and lang.lower() != "auto":
        cmd += ["-l", lang]

    log(f"转写中（{label}）…")
    if ws:
        ws.heartbeat()
    t0 = time.time()
    rc, out, err = run(cmd, timeout=7200)
    cost = time.time() - t0
    if ws:
        ws.heartbeat()

    text = ANSI_RE.sub("", out).strip()
    if rc != 0 and not text:
        return None, f"rc={rc} {err.strip()[:300]}"
    if not text:
        return None, "转写结果为空"
    log(f"转写完成，{cost:.1f}s，{len(text)} 字")
    return text, None


def transcribe(wav, cfg, ws=None):
    exe = paths.vibe_server(cfg)
    model = paths.model_path(cfg)
    if not exe.exists():
        return None, f"找不到转写引擎 {exe}。先跑 python setup.py"
    if not model.exists():
        return None, f"找不到语音模型 {model}。先跑 python setup.py"

    lang = (cfg.get("language") or "").strip()

    # 优先显卡；显卡不可用时 resolve_device 已经自动给出 CPU
    idx, desc, degraded = gpu.resolve_device(cfg)
    if degraded and idx < 0:
        log("没有检测到可用显卡，走 CPU（会慢不少）")

    text, err = _run_transcribe(exe, model, wav, idx, lang, ws, desc)
    if text:
        return text, None

    # 显卡跑挂了，兜底用 CPU 再试一次，别让整个流程失败
    if idx >= 0:
        log(f"显卡转写失败（{err[:120]}），改用 CPU 重试…")
        text, err2 = _run_transcribe(exe, model, wav, -1, lang, ws, "CPU 兜底")
        if text:
            log("CPU 兜底成功。建议把 config.json 的 gpu_device 改成 -1 省得每次重试")
            return text, None
        return None, f"显卡和 CPU 都失败。显卡：{err}；CPU：{err2}"

    return None, f"转写失败：{err}"


# ---------- 自检 ----------

def check_env(cfg):
    problems, notes = [], []

    import shutil
    if sys.version_info < (3, 9):
        problems.append(f"Python 版本过低（{sys.version.split()[0]}），需要 3.9+")

    if paths.vibe_server(cfg).exists():
        notes.append(f"转写引擎：{paths.vibe_server(cfg).name}")
    else:
        problems.append(f"缺转写引擎 {paths.vibe_server(cfg)}")

    mp = paths.model_path(cfg)
    if mp.exists():
        notes.append(f"语音模型：{mp.name}（{mp.stat().st_size / 1048576:.0f} MB）")
    else:
        problems.append(f"缺语音模型 {mp}")

    if paths.ffmpeg(cfg):
        notes.append("ffmpeg：就位")
    else:
        problems.append("缺 ffmpeg")

    if shutil.which("node"):
        rc, out, _ = run(["node", "-v"], timeout=30)
        notes.append(f"node {out.strip()}")
    else:
        problems.append("缺 node（抖音解析需要 Node 22+）")

    if douyin.NODE_SCRIPT.exists():
        notes.append("抖音抓取器：就位")
    else:
        problems.append(f"缺抖音抓取器 {douyin.NODE_SCRIPT.name}")

    # 显卡：优先用显卡，没有就提示会降级，不算问题
    g = gpu.summary(cfg)
    if g["gpus"]:
        for item in g["gpus"]:
            notes.append(f"检测到显卡 #{item['index']}：{item['description']}")
        if g["chosen_index"] < 0:
            notes.append("配置强制走 CPU")
        elif g["degraded"]:
            notes.append(f"配置指定的显卡不存在，已改用 #{g['chosen_index']}")
        else:
            notes.append(f"转写加速：{g['chosen_desc']}")
    else:
        notes.append("未检测到可用显卡，将走 CPU，速度约为显卡的 1/8")

    for n in notes:
        log(n)
    return problems


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="*", help="分享文案或链接")
    ap.add_argument("--lang", default=None)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument("--agent", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--work-dir", default=None, help="数据与输出目录，默认当前目录")
    args = ap.parse_args()

    cfg = paths.load_config()
    if args.work_dir:
        cfg["work_dir"] = args.work_dir
        os.chdir(args.work_dir)

    ws = Workspace(cfg=cfg, agent_name=args.agent)

    if args.cleanup:
        n = ws.cleanup_temp(keep_hours=cfg.get("keep_video_hours", 24))
        print(json.dumps({"ok": True, "removed": n}, ensure_ascii=False))
        return 0

    if args.check:
        problems = check_env(cfg)
        print(json.dumps({"ok": not problems, "problems": problems,
                          "gpu": gpu.summary(cfg),
                          "work_dir": str(paths.work_dir(cfg))}, ensure_ascii=False))
        return 0 if not problems else 1

    raw = " ".join(args.text).strip()
    url = extract_url(raw)
    if not url:
        print(json.dumps({"ok": False, "stage": "parse",
                          "error": "没从输入里找到链接"}, ensure_ascii=False))
        return 2

    ok, msg = ws.acquire(force=args.force)
    if not ok:
        print(json.dumps({"ok": False, "stage": "busy", "error": msg},
                         ensure_ascii=False))
        return 6
    try:
        return _run(url, cfg, ws, args)
    finally:
        ws.release()


def _run(url, cfg, ws, args):
    log(f"链接：{url}")
    if args.lang:
        cfg["language"] = args.lang

    paths.ensure_dirs(cfg)
    tmp = paths.temp_dir(cfg)
    media_dir = tmp / "media"

    if douyin.is_douyin(url):
        log("识别为抖音，走浏览器解析…")
        try:
            aweme_id = douyin.resolve_aweme_id(url)
        except Exception as e:
            aweme_id = None
            log(f"短链跳转失败：{e}")
        if not aweme_id:
            print(json.dumps({"ok": False, "stage": "parse",
                              "error": "没能从链接里解析出作品 id"}, ensure_ascii=False))
            return 2
        log(f"作品 id：{aweme_id}")

        aweme, err = douyin.fetch_detail(aweme_id, profile_dir=tmp / "browser_profile")
        if not aweme:
            print(json.dumps({"ok": False, "stage": "download", "error": err,
                              "aweme_id": aweme_id}, ensure_ascii=False))
            return 3
        meta = douyin.to_meta(aweme)
        log(f"标题：{meta['title'][:40] or '(无)'}　作者：{meta['uploader'] or '(未知)'}")

        log("下载视频…")
        video, err = douyin.download(aweme.get("play_urls"), media_dir / f"{aweme_id}.mp4")
        if not video:
            print(json.dumps({"ok": False, "stage": "download", "error": err,
                              "meta": meta}, ensure_ascii=False))
            return 3
    else:
        if not ytdlp_bin(cfg).exists():
            print(json.dumps({"ok": False, "stage": "download",
                              "error": "非抖音链接需要 yt-dlp，先跑 python setup.py"},
                             ensure_ascii=False))
            return 3
        meta = ytdlp_meta(url, cfg)
        log(f"标题：{meta.get('title') or '(未知)'}　作者：{meta.get('uploader') or '(未知)'}")
        video, err = ytdlp_download(url, cfg, media_dir)
        if not video:
            print(json.dumps({"ok": False, "stage": "download", "error": err,
                              "meta": meta}, ensure_ascii=False))
            return 3

    log(f"已下载：{video.name}（{video.stat().st_size / 1048576:.1f} MB）")
    meta["video_path"] = str(video)
    ws.heartbeat()

    wav, err = to_wav(video, cfg, tmp)
    if not wav:
        print(json.dumps({"ok": False, "stage": "audio", "error": err,
                          "meta": meta}, ensure_ascii=False))
        return 4

    transcript, err = transcribe(wav, cfg, ws)
    if not transcript:
        print(json.dumps({"ok": False, "stage": "transcribe", "error": err,
                          "meta": meta}, ensure_ascii=False))
        return 5

    vid = meta.get("id") or video.stem
    txt_path = ws.transcript_path(vid)
    txt_path.write_text(transcript, encoding="utf-8")

    meta["transcript_chars"] = len(transcript)
    meta["processed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    ws.meta_path(vid).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        if wav.exists():
            wav.unlink()
    except Exception:
        pass

    summary, usable = guess_summary(meta.get("title") or meta.get("description") or "")
    ts = meta.get("create_ts") or time.time()
    date_str = time.strftime("%Y%m%d", time.localtime(ts))

    print(json.dumps({
        "ok": True,
        "id": vid,
        "url": url,
        "title": meta.get("title"),
        "uploader": meta.get("uploader"),
        "duration": meta.get("duration"),
        "like_count": meta.get("like_count"),
        "comment_count": meta.get("comment_count"),
        "share_count": meta.get("share_count"),
        "collect_count": meta.get("collect_count"),
        "upload_date": meta.get("upload_date"),
        "date_str": date_str,
        "transcript_path": str(txt_path),
        "meta_path": str(ws.meta_path(vid)),
        "video_path": str(video),
        "chars": len(transcript),
        "suggested_summary": summary,
        "summary_usable": usable,
        "output_dir": str(paths.output_dir(cfg)),
        "already_done": bool(ws.find_done(vid)),
        "preview": transcript[:120],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
