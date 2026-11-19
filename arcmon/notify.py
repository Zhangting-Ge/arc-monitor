"""把事件整理成中文推送，并通过 Bark 发到 iPhone。"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass

import requests

from .config import SITES, BarkTarget, Subscriber, discount_pct

log = logging.getLogger(__name__)

MAX_WATCH_PUSHES = 6      # 每人每次运行最多几条单独的关注提醒，多的合并
MAX_FEED_LINES = 10
BODY_LIMIT = 1800         # iOS 推送内容有大小上限，留余量


@dataclass
class Push:
    title: str
    body: str
    subtitle: str | None = None
    url: str | None = None
    icon: str | None = None
    group: str = "始祖鸟"
    level: str = "active"  # active / timeSensitive / passive


def eur(x) -> str:
    if x is None:
        return "?"
    x = float(x)
    return f"€{x:.0f}" if x.is_integer() else f"€{x:.2f}"


def price_text(lo, hi=None, regular=None) -> str:
    s = eur(lo) + ("起" if hi is not None and hi > lo else "")
    pct = discount_pct(lo, regular)
    if pct:
        s += f"（原价{eur(regular)}，-{pct}%）"
    return s


def _clip(body: str) -> str:
    return body if len(body) <= BODY_LIMIT else body[:BODY_LIMIT - 1] + "…"


def sizes_text(keys: list[str]) -> str:
    """["Black|M", "Black|L", "Forage|M"] -> "Black：M、L；Forage：M" """
    by_colour: dict[str, list[str]] = defaultdict(list)
    for k in keys:
        colour, _, size = k.partition("|")
        by_colour[colour].append(size)
    if len(by_colour) == 1:
        (colour, sizes), = by_colour.items()
        if sizes == ["均码"]:
            return colour
        return f"{'、'.join(sizes)}（{colour}）"
    return "；".join(f"{c}：{'、'.join(s)}" for c, s in by_colour.items())


# ---------------- 全站动态 ----------------
_FEED_ORDER = {"new_product": 0, "back_in_catalog": 1, "price_drop": 2, "new_colour": 3}
_FEED_NAME = {"new_product": "上新", "back_in_catalog": "重新上架", "price_drop": "降价", "new_colour": "新配色"}


def feed_line(ev: dict) -> str:
    t = ev["type"]
    if t == "new_product":
        return f"🆕 {ev['name']} {price_text(ev['min'], ev['max'], ev['price'])}"
    if t == "back_in_catalog":
        return f"↩️ {ev['name']} 重新上架 {price_text(ev['min'], ev['max'], ev['price'])}"
    if t == "price_drop":
        pct = discount_pct(ev["min"], ev["price"])
        if not pct:
            tail = ""
        elif ev["old_min"] == ev["price"]:
            tail = f"（-{pct}%）"
        else:
            tail = f"（原价{eur(ev['price'])}，-{pct}%）"
        return f"📉 {ev['name']} {eur(ev['old_min'])}→{eur(ev['min'])}{tail}"
    if t == "new_colour":
        return f"🎨 {ev['name']} 新配色：{'、'.join(ev['new_colours'])}"
    return f"{t} {ev.get('name')}"


def feed_pushes(events: list[dict], country: str, language: str, more_hint: str) -> list[Push]:
    out = []
    by_site: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        by_site[ev["site"]].append(ev)
    for site, evs in by_site.items():
        evs.sort(key=lambda e: (_FEED_ORDER.get(e["type"], 9), -discount_pct(e.get("min"), e.get("price"))))
        counts: dict[str, int] = defaultdict(int)
        for e in evs:
            counts[e["type"]] += 1
        label = SITES[site]["label"]
        title = f"{label}：" + " · ".join(
            f"{_FEED_NAME[t]} {n}" for t, n in sorted(counts.items(), key=lambda kv: _FEED_ORDER[kv[0]]))
        lines = [feed_line(e) for e in evs[:MAX_FEED_LINES]]
        if len(evs) > MAX_FEED_LINES:
            lines.append(f"…还有 {len(evs) - MAX_FEED_LINES} 条{more_hint}")
        base = f"https://{SITES[site]['host']}/{country}/{language}"
        single = evs[0] if len(evs) == 1 else None
        out.append(Push(
            title=title,
            body=_clip("\n".join(lines)),
            url=f"{base}/shop/{single['slug']}" if single else
                (f"{base}/c/new-arrivals" if counts.get("new_product") else f"{base}/"),
            icon=single.get("img") if single else None,
            group="始祖鸟·上新",
        ))
    return out


# ---------------- 关注提醒 ----------------
_WATCH_TITLE = {
    "watch_restock": "🔔 补货了",
    "watch_match": "✅ 新上架符合条件",
    "watch_price": "📉 关注商品降价",
}


def watch_push(ev: dict) -> Push:
    label = SITES[ev["site"]]["label"]
    low = "（库存紧张）" if ev.get("low") else ""
    if ev["type"] == "watch_restock":
        line1 = f"{sizes_text(ev['gained'])} 有货了{low}"
    elif ev["type"] == "watch_price":
        line1 = f"{eur(ev['old_price'])}→{eur(ev['price'])}，有货：{sizes_text(ev['avail'])}"
    else:
        line1 = f"有货：{sizes_text(ev['avail'])}{low}"
    body = f"{line1}\n{price_text(ev['price'], None, ev.get('regular'))} · 关注：{ev['rule']}"
    return Push(
        title=f"{_WATCH_TITLE[ev['type']]} · {label}",
        subtitle=ev["name"],
        body=_clip(body),
        url=ev["url"],
        icon=ev.get("img"),
        group="始祖鸟·关注",
        level="timeSensitive",
    )


def watch_pushes(events: list[dict]) -> list[Push]:
    pushes = [watch_push(e) for e in events[:MAX_WATCH_PUSHES]]
    rest = events[MAX_WATCH_PUSHES:]
    if rest:
        lines = [f"• {e['name']}（{SITES[e['site']]['label']}）{sizes_text(e['avail'])} {eur(e['price'])}" for e in rest]
        pushes.append(Push(title=f"🔔 另有 {len(rest)} 件关注商品有变化", body=_clip("\n".join(lines)),
                           group="始祖鸟·关注", level="timeSensitive"))
    return pushes


def initial_push(items: list[tuple[str, list[dict]]]) -> Push:
    """items: [(规则名, [有货的商品条目...]), ...]"""
    blocks = []
    for rule_name, entries in items:
        if not entries:
            blocks.append(f"【{rule_name}】暂时没有符合条件的有货商品，有了会提醒你")
            continue
        entries = sorted(entries, key=lambda e: e.get("p") or 0)
        lines = [f"【{rule_name}】{len(entries)} 款有货"]
        for e in entries[:4]:
            lines.append(f"• {e['name']}（{SITES[e['site']]['label']}）{sizes_text(e['a'])} {eur(e['p'])}")
        if len(entries) > 4:
            lines.append(f"…等 {len(entries)} 款")
        blocks.append("\n".join(lines))
    return Push(title="📋 关注清单已生效", body=_clip("\n".join(blocks)), group="始祖鸟·关注")


# ---------------- Bark ----------------
class Bark:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.sent: list[tuple[str, Push]] = []

    def send(self, sub: Subscriber, push: Push) -> bool:
        self.sent.append((sub.name, push))
        target: BarkTarget | None = sub.bark
        if self.dry_run or target is None:
            why = "dry-run" if self.dry_run else "未配置 Bark"
            print(f"\n[{why}] → {sub.name}\n  {push.title}"
                  + (f"\n  {push.subtitle}" if push.subtitle else "")
                  + "\n  " + push.body.replace("\n", "\n  ")
                  + (f"\n  🔗 {push.url}" if push.url else ""))
            return True
        payload = {"device_key": target.key, "title": push.title, "body": push.body,
                   "group": push.group, "level": push.level, "isArchive": 1}
        for k in ("subtitle", "url", "icon"):
            if getattr(push, k):
                payload[k] = getattr(push, k)
        for attempt in range(2):
            try:
                r = requests.post(f"{target.server}/push", json=payload, timeout=15)
                if r.status_code == 200 and r.json().get("code") == 200:
                    return True
                log.error("Bark 推送失败 %s: %s %s", sub.name, r.status_code, r.text[:200])
                if 400 <= r.status_code < 500:
                    return False   # key 错误之类，重试也没用
            except Exception as e:  # noqa: BLE001
                log.error("Bark 推送异常 %s: %s", sub.name, e)
            if attempt == 0:
                time.sleep(3)
        return False
