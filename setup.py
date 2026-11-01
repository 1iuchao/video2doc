#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
环境自举：把转写引擎和语音模型装到 engine/ 目录。

用法：
    python setup.py --check            只看现状和缺什么，不下载
    python setup.py --install          缺什么装什么（默认 small 模型）
    python setup.py --install --model medium
    python setup.py --from "D:\\Vibe"   从本地已有目录拷贝（最快，推荐）
    python setup.py --verify           用一段合成音频真跑一次转写，确认能用
    python setup.py --install --force  忽略已存在的文件，重下

设计取舍：模型接近 500MB 以上，从公共镜像下只有百来 KB/s，要几十分钟。
所以 --from 本地拷贝是首选路径，--install 只是没有可拷来源时的兜底。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import gpu
import paths

CHUNK = 1024 * 512

# GitHub Release 上的资产名。改成你自己仓库的地址即可。
DEFAULT_RELEASE = "https://github.com/1iuchao/video2doc/releases/download/v1.0.0"
ENGINE_ZIP = "engine-win64.zip"

# 公共镜像兜底（慢，但不用自己发 release 也能装）
MODEL_MIRROR = "https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/{}"

MODELS = {
    "small": ("ggml-small.bin", 487601967),
    "medium": ("ggml-medium.bin", 1533774781),
}

GH_PROXY = "https://gh-proxy.com/"


def log(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def gh_mirror(url):
    """GitHub 直链在国内基本下不动，套一层加速。"""
    if "github.com" in url and not url.startswith(GH_PROXY):
        return GH_PROXY + url
    return url


def probe(url, timeout=15):
    """HEAD 探测，返回 (ok, 内容长度)。"""
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, int(r.headers.get("Content-Length") or 0)
    except Exception:
        return False, 0


def probe_any(url, timeout=15):
    """先直连，不通再走镜像。

    国内直连 GitHub 多半不通，如果只探测直连就会误判成「Release 里没这个文件」，
    从而退回更慢的公共镜像。返回 (ok, length, 实际可用的 url)。
    """
    candidates = [url]
    mirrored = gh_mirror(url)
    if mirrored != url:
        candidates.append(mirrored)
    for u in candidates:
        ok, n = probe(u, timeout)
        if ok:
            return True, n, u
    return False, 0, url


def download(url, dest, expected=None, label=""):
    """带断点续传的下载。先直连，失败自动换 GitHub 镜像。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    candidates = [url, gh_mirror(url)]
    if url.startswith(GH_PROXY):
        candidates = [url, url[len(GH_PROXY):]]

    for i, u in enumerate(candidates):
        pos = dest.stat().st_size if dest.exists() else 0
        if expected and pos >= expected:
            log(f"  已存在，跳过（{human(pos)}）")
            return True
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            if pos:
                req.add_header("Range", f"bytes={pos}-")
                log(f"  从 {human(pos)} 处续传")
            t0 = time.time()
            last = 0
            with urllib.request.urlopen(req, timeout=60) as r:
                total = expected or int(r.headers.get("Content-Length") or 0) + pos
                mode = "ab" if (pos and r.status == 206) else "wb"
                if mode == "wb":
                    pos = 0
                with open(dest, mode) as f:
                    got = pos
                    while True:
                        chunk = r.read(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if time.time() - last > 1.5:
                            last = time.time()
                            sp = (got - pos) / max(time.time() - t0, 0.001)
                            pct = f"{got / total * 100:.0f}%" if total else "--"
                            log(f"  {label} {pct}  {human(got)}/{human(total)}  {human(sp)}/s")
            size = dest.stat().st_size
            if expected and size < expected * 0.999:
                log(f"  大小不符：{human(size)} / 期望 {human(expected)}")
                if i + 1 < len(candidates):
                    continue
                return False
            log(f"  完成 {human(size)}")
            return True
        except Exception as e:
            log(f"  下载失败（{type(e).__name__}）：{str(e)[:120]}")
            if i + 1 < len(candidates):
                log("  换镜像重试…")
                continue
            return False
    return False


def copy_from(src_dir, cfg, model_name="small"):
    """从本地已装的 Vibe（或任意含同款文件的目录）拷引擎和模型。"""
    src = Path(src_dir).expanduser()
    if not src.exists():
        return False, f"源目录不存在：{src}"

    engine = paths.engine_dir(cfg)
    engine.mkdir(parents=True, exist_ok=True)
    model_file = MODELS.get(model_name, MODELS["small"])[0]

    done, missing = [], []
    for name in ("vibe-server.exe", "ffmpeg.exe"):
        s = src / name
        if s.exists():
            shutil.copy2(s, engine / name)
            done.append(f"{name}（{human(s.stat().st_size)}）")
        else:
            missing.append(name)

    # 模型可能在源目录、data/models、或者用户 AppData
    model_candidates = [
        src / model_file,
        src / "data" / "models" / model_file,
        src / "models" / model_file,
        Path(os.environ.get("LOCALAPPDATA", "")) / "github.com.thewh1teagle.vibe" / model_file,
    ]
    # 源目录里的任何 .bin 都比没有强
    model_candidates += sorted(src.glob("*.bin"))
    if (src / "data" / "models").exists():
        model_candidates += sorted((src / "data" / "models").glob("*.bin"))

    for c in model_candidates:
        if c and c.exists() and c.suffix == ".bin":
            dest = paths.model_path(cfg)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(c, dest)
            done.append(f"{c.name} -> {dest.name}（{human(c.stat().st_size)}）")
            break
    else:
        missing.append(f"{model_file}")

    # yt-dlp 可选，有就带上
    ytdlp = paths.engine_dir(cfg) / "yt-dlp.exe"
    if not ytdlp.exists():
        for c in [src / "yt-dlp.exe", Path.cwd() / "bin" / "yt-dlp.exe"]:
            if c.exists():
                shutil.copy2(c, ytdlp)
                done.append("yt-dlp.exe")
                break

    msg = "已拷贝：" + ("；".join(done) if done else "无")
    if missing:
        msg += f"　缺失：{', '.join(missing)}"
    return not missing, msg


def install(cfg, model_name="small", force=False):
    engine = paths.engine_dir(cfg)
    engine.mkdir(parents=True, exist_ok=True)
    base = (cfg.get("release_base") or DEFAULT_RELEASE).rstrip("/")

    ok_any = True

    # 1. 引擎包
    need_engine = force or not paths.vibe_server(cfg).exists()
    if need_engine:
        if not base:
            log("config.json 里 release_base 是空的，跳过引擎下载。")
            log("处理办法：跑 python setup.py --from <本地已有 Vibe 目录>，")
            log("或者把 config.json 的 release_base 改成你自己仓库的 Release 地址。")
            ok_any = False
        else:
            z = engine / ENGINE_ZIP
            log(f"下载引擎包 {ENGINE_ZIP} …")
            if download(f"{base}/{ENGINE_ZIP}", z, label="engine"):
                try:
                    with zipfile.ZipFile(z) as zf:
                        zf.extractall(engine)
                    z.unlink()
                    log("  已解压")
                except Exception as e:
                    log(f"  解压失败：{e}")
                    ok_any = False
            else:
                ok_any = False
    else:
        log("引擎已存在，跳过")

    # 2. 模型
    mp = paths.model_path(cfg)
    if not force and mp.exists() and mp.stat().st_size > 100 * 1024 * 1024:
        log(f"模型已存在（{human(mp.stat().st_size)}），跳过")
        return ok_any

    name, size = MODELS.get(model_name, MODELS["small"])
    mp.parent.mkdir(parents=True, exist_ok=True)

    # 先试 release（快），再试公共镜像（慢）
    tried_release = False
    if base:
        ok, length, real_url = probe_any(f"{base}/{name}")
        if ok:
            tried_release = True
            log(f"从 Release 下载模型 {name}（{human(length or size)}）…")
            if download(real_url, mp, expected=length or size, label="model"):
                return True
            log("  Release 下载失败，改走公共镜像…")

    if not tried_release:
        log(f"从公共镜像下载模型 {name}（{human(size)}），速度约 100–200 KB/s，预计几十分钟…")
        log("  可以随时 Ctrl+C 中断，下次运行会续传。")
    if not download(MODEL_MIRROR.format(name), mp, expected=size, label="model"):
        ok_any = False

    return ok_any


def verify(cfg):
    """用一段合成音频真跑一次转写。能出结果才算真的装好了。"""
    ffmpeg = paths.ffmpeg(cfg)
    exe = paths.vibe_server(cfg)
    model = paths.model_path(cfg)

    if not ffmpeg or not exe.exists() or not model.exists():
        print(json.dumps({"ok": False, "error": "引擎或模型还没装好，先跑 --install 或 --from"},
                         ensure_ascii=False))
        return 2

    tmp = paths.data_dir(cfg) / "中间文件"
    tmp.mkdir(parents=True, exist_ok=True)
    wav = tmp / "_selftest.wav"

    log("生成测试音频…")
    rc = subprocess.run([str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                         "-ac", "1", "-ar", "16000", str(wav)],
                        capture_output=True)
    if rc.returncode != 0 or not wav.exists():
        print(json.dumps({"ok": False, "error": "ffmpeg 无法生成测试音频"},
                         ensure_ascii=False))
        return 3

    log("跑一次转写（大约 5–30 秒）…")
    idx, desc, _ = gpu.resolve_device(cfg)
    log(f"  设备：{desc}")
    t0 = time.time()
    p = subprocess.run([str(exe), "transcribe", str(model), str(wav),
                        "--gpu-device", str(idx), "-l", "zh"],
                       capture_output=True, encoding="utf-8", errors="replace",
                       timeout=600)
    cost = time.time() - t0
    try:
        wav.unlink()
    except Exception:
        pass

    if p.returncode != 0:
        hint = "先试试把 config.json 的 gpu_device 改成 -1，如果 CPU 能跑通说明是显卡驱动的问题"
        print(json.dumps({
            "ok": False,
            "device": desc,
            "error": f"转写进程返回 {p.returncode}",
            "stderr": (p.stderr or "")[:500],
            "hint": hint,
        }, ensure_ascii=False))
        return 4

    print(json.dumps({
        "ok": True,
        "cost_sec": round(cost, 1),
        "device": desc,
        "backend": "CPU" if idx < 0 else "GPU/Vulkan",
        "model": model.name,
        "engine": exe.name,
        "note": "测试音频是正弦波，转出空文本或噪声都正常——能跑完不报错即代表引擎可用",
    }, ensure_ascii=False))
    return 0


def report(cfg):
    g = gpu.summary(cfg)
    info = {
        "work_dir": str(paths.work_dir(cfg)),
        "engine_dir": str(paths.engine_dir(cfg)),
        "engine": paths.vibe_server(cfg).exists(),
        "ffmpeg": bool(paths.ffmpeg(cfg)),
        "model": paths.model_path(cfg).exists(),
        "model_size": human(paths.model_path(cfg).stat().st_size)
        if paths.model_path(cfg).exists() else "0",
        "ytdlp": (paths.engine_dir(cfg) / "yt-dlp.exe").exists(),
        "node": bool(shutil.which("node")),
        "gpu": g,
        "ready": False,
    }
    info["ready"] = info["engine"] and info["ffmpeg"] and info["model"]

    if info["ready"]:
        if g["chosen_index"] >= 0:
            log(f"转写将使用显卡：{g['chosen_desc']}")
        else:
            log("未检测到可用显卡，转写将走 CPU（约慢 8 倍）")

    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0 if info["ready"] else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--from", dest="from_dir", default=None)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--model", default="small", choices=list(MODELS))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    cfg = paths.load_config()

    if args.check or not any([args.install, args.from_dir, args.verify]):
        return report(cfg)

    if args.from_dir:
        ok, msg = copy_from(args.from_dir, cfg, args.model)
        log(msg)
        return report(cfg) if ok else 1

    if args.install:
        ok = install(cfg, args.model, args.force)
        report(cfg)
        return 0 if ok else 1

    if args.verify:
        return verify(cfg)

    return 0


if __name__ == "__main__":
    sys.exit(main())
