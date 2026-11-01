#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
共享工作区的协作机制：并发心跳、去重、输出命名、索引。

同一个工作目录下可能有多个 agent 同时干活，靠这个模块避免互相踩踏。
设计原则：状态全部用「文件 + 追加写」表达，不用锁、不用数据库，
任一 agent 崩溃都不会把工作区锁死。
"""

import csv
import getpass
import json
import os
import re
import socket
import time
from pathlib import Path

import paths

BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')

INDEX_COLUMNS = ["处理时间", "作品id", "作者", "标题", "时长秒",
                 "点赞", "原文文件", "分析文件", "链接"]


def now_str():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def today_str():
    return time.strftime("%Y%m%d")


def sanitize(text, limit=20, fallback="未命名"):
    """把任意文本变成安全的文件名片段。"""
    if not text:
        return fallback
    s = BAD_CHARS.sub("_", str(text))
    s = re.sub(r"#[^#\s]+", " ", s)
    s = re.sub(r"@\S+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ._")
    s = re.sub(r"[，。！？、,.!?；;：:]+$", "", s).strip()
    if not s:
        return fallback
    return s[:limit].strip(" ._") or fallback


def guess_summary(desc, limit=20):
    """从视频标题猜一个可用概括。返回 (概括, 是否可用)。"""
    s = sanitize(desc, limit=limit, fallback="")
    if len(s) < 6:
        return s, False
    return s, True


class Workspace:
    def __init__(self, cfg=None, agent_name=None):
        self.cfg = cfg or paths.load_config()
        self.data = paths.data_dir(self.cfg)
        self.output_dir = paths.output_dir(self.cfg)
        self.temp_dir = paths.temp_dir(self.cfg)
        self.state_file = self.data / "工作区状态.json"
        self.index_file = self.data / "索引.csv"
        self.timeout = int(self.cfg.get("heartbeat_timeout") or 600)
        self.agent_name = agent_name or self._default_name()

    @staticmethod
    def _default_name():
        try:
            user = getpass.getuser()
        except Exception:
            user = "user"
        return f"{socket.gethostname()}/{user}/pid{os.getpid()}"

    def _ensure(self):
        for d in (self.data, self.output_dir, self.temp_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---------- 并发心跳 ----------

    def is_busy(self):
        if not self.state_file.exists():
            return False, {}
        try:
            info = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return False, {}
        age = time.time() - (info.get("beat_ts") or 0)
        if age > self.timeout:
            return False, {"stale": True, "age_sec": int(age), **info}
        return True, {"age_sec": int(age), **info}

    def acquire(self, force=False):
        self._ensure()
        busy, info = self.is_busy()
        if busy and not force:
            return False, (
                f"另一个 agent 正在工作：{info.get('agent')}　"
                f"已运行 {info.get('age_sec')} 秒　开始于 {info.get('started')}"
            )
        payload = {
            "agent": self.agent_name,
            "pid": os.getpid(),
            "started": now_str(),
            "beat": now_str(),
            "beat_ts": time.time(),
            "forced": bool(force and busy),
        }
        self.state_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return True, "已占位"

    def heartbeat(self):
        try:
            info = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            info = {"agent": self.agent_name, "started": now_str(), "pid": os.getpid()}
        info["beat"] = now_str()
        info["beat_ts"] = time.time()
        self.state_file.write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    def release(self):
        if not self.state_file.exists():
            return
        try:
            info = json.loads(self.state_file.read_text(encoding="utf-8"))
            if info.get("agent") != self.agent_name and info.get("pid") != os.getpid():
                return
        except Exception:
            pass
        try:
            self.state_file.unlink()
        except Exception:
            pass

    # ---------- 索引与去重 ----------

    def read_index(self):
        if not self.index_file.exists():
            return []
        try:
            with self.index_file.open("r", encoding="utf-8-sig", newline="") as f:
                return list(csv.DictReader(f))
        except Exception:
            return []

    def find_done(self, aweme_id):
        if not aweme_id:
            return None
        for row in self.read_index():
            if str(row.get("作品id", "")).strip() == str(aweme_id):
                return row
        return None

    def add_index(self, record):
        self._ensure()
        new_file = not self.index_file.exists()
        row = {k: record.get(k, "") for k in INDEX_COLUMNS}
        row["处理时间"] = row["处理时间"] or now_str()
        with self.index_file.open("a", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=INDEX_COLUMNS)
            if new_file:
                w.writeheader()
            w.writerow(row)
        return self.index_file

    # ---------- 输出命名 ----------

    def make_names(self, date_str, summary):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        base = f"{date_str}_{sanitize(summary)}"
        stem, n = base, 1
        while any((self.output_dir / f"{stem}_{k}.md").exists() for k in ("原文", "分析")):
            n += 1
            stem = f"{base}_{n}"
        return (self.output_dir / f"{stem}_原文.md",
                self.output_dir / f"{stem}_分析.md")

    # ---------- 中间文件 ----------

    def transcript_path(self, vid):
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        return self.temp_dir / f"{vid}.原文原始.txt"

    def meta_path(self, vid):
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        return self.temp_dir / f"{vid}.meta.json"

    def cleanup_temp(self, keep_hours=24):
        if not self.temp_dir.exists():
            return 0
        cutoff = time.time() - keep_hours * 3600
        n = 0
        for p in self.temp_dir.iterdir():
            if not p.is_file():
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
            except Exception:
                pass
        return n


if __name__ == "__main__":
    ws = Workspace()
    busy, info = ws.is_busy()
    print(json.dumps({
        "work_dir": str(paths.work_dir(ws.cfg)),
        "data_dir": str(ws.data),
        "output_dir": str(ws.output_dir),
        "busy": busy,
        "info": info,
        "index_rows": len(ws.read_index()),
    }, ensure_ascii=False, indent=2))
