"""状态文件：每次运行的快照存在 state/ 下，GitHub Actions 有变化时提交回仓库。

内容只在真正有变化时才改写（不写入每次运行的时间戳），避免每 10 分钟产生一个提交。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


class State:
    def __init__(self, directory: str = "state"):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        self._orig: dict[str, str] = {}
        self.catalog: dict[str, dict | None] = {}
        self.watch: dict = self._load("watch.json") or {}
        self.meta: dict = self._load("meta.json") or {}

    def _path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def _load(self, name: str):
        p = self._path(name)
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            text = f.read()
        self._orig[name] = text
        return json.loads(text)

    def get_catalog(self, site: str) -> dict | None:
        if site not in self.catalog:
            self.catalog[site] = self._load(f"catalog_{site}.json")
        return self.catalog[site]

    def _write(self, name: str, obj) -> bool:
        text = _dump(obj)
        if self._orig.get(name) == text:
            return False
        with open(self._path(name), "w", encoding="utf-8") as f:
            f.write(text)
        self._orig[name] = text
        return True

    def save(self) -> list[str]:
        changed = []
        for site, cat in self.catalog.items():
            if cat is not None and self._write(f"catalog_{site}.json", cat):
                changed.append(f"catalog_{site}.json")
        if self._write("watch.json", self.watch):
            changed.append("watch.json")
        if self._write("meta.json", self.meta):
            changed.append("meta.json")
        return changed

    # ---------- 事件日志（给每日汇总用） ----------
    def log_events(self, events: list[dict], keep_days: int = 8):
        p = self._path("events.jsonl")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        lines = []
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        kept = [ln for ln in lines if json.loads(ln).get("t", "") >= cutoff]
        if not events and len(kept) == len(lines):
            return
        kept += [json.dumps(e, ensure_ascii=False, sort_keys=True) for e in events]
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + ("\n" if kept else ""))

    def read_events(self, since: datetime) -> list[dict]:
        p = self._path("events.jsonl")
        if not os.path.exists(p):
            return []
        s = since.isoformat()
        with open(p, encoding="utf-8") as f:
            return [e for e in (json.loads(ln) for ln in f if ln.strip()) if e.get("t", "") >= s]
