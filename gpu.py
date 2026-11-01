#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
显卡探测与设备选择。

原则：**能上显卡就上显卡，没有就老实走 CPU，别报错**。

vibe-server 底层是 whisper.cpp + ggml，支持的加速后端是 Vulkan，
所以 Intel Arc、AMD、NVIDIA 的独显/核显只要驱动支持 Vulkan 都能被识别，
不限于 CUDA。

config.json 里 gpu_device 支持三种写法：
    "auto"  自动选第一块可用 GPU，没有就用 CPU（默认，推荐）
    0/1/2   强制指定某块
    -1      强制 CPU
"""

import json
import subprocess
from functools import lru_cache

import paths


@lru_cache(maxsize=8)
def _raw_devices(exe_path_str, timeout=60):
    """调 vibe-server devices，结果按 exe 路径缓存（每次调用约 1–2 秒）。"""
    try:
        p = subprocess.run([exe_path_str, "devices"], capture_output=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        if p.returncode != 0:
            return []
        return json.loads(p.stdout or "[]")
    except Exception:
        return []


def detect_gpus(cfg=None):
    """返回可用 GPU 列表 [{"index":0,"name":"Vulkan0","description":"..."}]。"""
    cfg = cfg or paths.load_config()
    exe = paths.vibe_server(cfg)
    if not exe.exists():
        return []
    devs = _raw_devices(str(exe))
    if not isinstance(devs, list):
        return []
    return [d for d in devs if str(d.get("type", "")).lower() == "gpu"]


def resolve_device(cfg=None):
    """决定用哪个设备。

    返回 (index, 描述, 是否自动降级)
        index = -1 表示走 CPU
    """
    cfg = cfg or paths.load_config()
    raw = cfg.get("gpu_device", "auto")
    gpus = detect_gpus(cfg)

    # 明确指定了数字
    if str(raw).strip().lower() not in ("", "auto", "none", "null"):
        try:
            idx = int(raw)
        except (TypeError, ValueError):
            idx = 0
        if idx == -1:
            return -1, "CPU（配置里强制指定）", False
        match = next((g for g in gpus if g.get("index") == idx), None)
        if match:
            return idx, match.get("description") or match.get("name") or f"GPU{idx}", False
        # 指定了但没找到，降级到第一块可用的
        if gpus:
            g = gpus[0]
            return g["index"], g.get("description") or g.get("name"), True
        return -1, "CPU（指定的显卡不存在，自动降级）", True

    # auto
    if gpus:
        g = gpus[0]
        return g["index"], g.get("description") or g.get("name") or "GPU", False
    return -1, "CPU（未检测到可用显卡）", True


def summary(cfg=None):
    """给自检用的完整报告。"""
    cfg = cfg or paths.load_config()
    gpus = detect_gpus(cfg)
    idx, desc, degraded = resolve_device(cfg)
    return {
        "gpus": [{"index": g.get("index"),
                  "name": g.get("name"),
                  "description": g.get("description")} for g in gpus],
        "chosen_index": idx,
        "chosen_desc": desc,
        "degraded": degraded,
        "backend": "CPU" if idx < 0 else "Vulkan/GPU",
    }


if __name__ == "__main__":
    print(json.dumps(summary(), ensure_ascii=False, indent=2))
