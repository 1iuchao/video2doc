#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
路径解析。所有模块都从这里拿路径，保证没有硬编码绝对路径。

三条约定：
1. 代码和引擎跟着 skill 走      -> SKILL_DIR
2. 数据和输出跟着当前工作目录走   -> WORK_DIR
3. 一切都可以被 config 或环境变量覆盖

这样一套 skill 装在 ~/.workbuddy/skills/ 里，可以在任意项目目录下用，
不同项目的数据互不干扰。
"""

import json
import os
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent

DEFAULT_CONFIG = {
    "work_dir": "",
    "data_dir": "视频分析数据",
    "output_dir": "aaa输出",
    "engine_dir": "engine",
    "model": "models/ggml-small.bin",
    "gpu_device": 0,
    "language": "zh",
    "keep_video_hours": 24,
    "heartbeat_timeout": 600,
    "release_base": "",
    "cookies_file": "",
    "proxy": "",
}


def find_config_file():
    """配置只从两处来：环境变量 > skill 目录。

    刻意不去读当前目录的 config.json——工作目录下可能恰好有别的项目的
    同名文件，误读会静默用错配置。
    """
    env = os.environ.get("DOUYIN_SKILL_CONFIG")
    if env and Path(env).expanduser().exists():
        return Path(env).expanduser()
    skill_cfg = SKILL_DIR / "config.json"
    if skill_cfg.exists():
        return skill_cfg
    return None


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = find_config_file()
    if path:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in raw.items() if not k.startswith("_")})
            cfg["_config_path"] = str(path)
        except Exception as e:
            cfg["_config_error"] = f"{path}: {e}"
    return cfg


def work_dir(cfg=None):
    cfg = cfg or load_config()
    raw = cfg.get("work_dir") or ""
    if raw:
        p = Path(os.path.expandvars(raw)).expanduser()
        return p if p.is_absolute() else (Path.cwd() / p)
    return Path.cwd()


def _under(base, raw, default):
    raw = raw or default
    p = Path(os.path.expandvars(str(raw))).expanduser()
    return p if p.is_absolute() else (base / p)


def data_dir(cfg=None):
    cfg = cfg or load_config()
    return _under(work_dir(cfg), cfg.get("data_dir"), "视频分析数据")


def output_dir(cfg=None):
    cfg = cfg or load_config()
    return _under(work_dir(cfg), cfg.get("output_dir"), "aaa输出")


def engine_dir(cfg=None):
    """引擎跟着 skill 走，不跟着工作目录。"""
    cfg = cfg or load_config()
    return _under(SKILL_DIR, cfg.get("engine_dir"), "engine")


def model_path(cfg=None):
    cfg = cfg or load_config()
    return _under(engine_dir(cfg), cfg.get("model"), "models/ggml-small.bin")


def vibe_server(cfg=None):
    return engine_dir(cfg) / "vibe-server.exe"


def ffmpeg(cfg=None):
    """引擎目录优先，其次系统 PATH。"""
    local = engine_dir(cfg) / "ffmpeg.exe"
    if local.exists():
        return local
    from shutil import which
    w = which("ffmpeg")
    return Path(w) if w else None


def temp_dir(cfg=None):
    return data_dir(cfg) / "中间文件"


def ensure_dirs(cfg=None):
    cfg = cfg or load_config()
    for d in (data_dir(cfg), output_dir(cfg), temp_dir(cfg), temp_dir(cfg) / "media"):
        d.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    cfg = load_config()
    print(json.dumps({
        "skill_dir": str(SKILL_DIR),
        "work_dir": str(work_dir(cfg)),
        "data_dir": str(data_dir(cfg)),
        "output_dir": str(output_dir(cfg)),
        "engine_dir": str(engine_dir(cfg)),
        "model": str(model_path(cfg)),
        "model_exists": model_path(cfg).exists(),
        "vibe_server_exists": vibe_server(cfg).exists(),
        "ffmpeg": str(ffmpeg(cfg) or ""),
        "config_file": cfg.get("_config_path"),
    }, ensure_ascii=False, indent=2))
