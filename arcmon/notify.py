"""把事件整理成中文推送，并通过 Bark 发到 iPhone。"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass

import requests

from .config import SITES, BarkTarget, Subscriber, size_matches

log = logging.getLogger(__name__)

MAX_WATCH_PUSHES = 6      # 每人每次运行最多几条单独的关注提醒，多的合并
MAX_FEED_ITEMS = 8        # 每条全站动态推送最多列几款，多的进日报
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


def zhe(price, base) -> str:
    """价格相对 base 是几折：0.7 -> "7折"，0.595 -> "5.9折"。没打折返回空串。"""
    if not price or not base or price >= base - 0.005:
        return ""
    z = round(price / base * 10, 1)
    return f"{z:g}折"


def price_text(lo, hi=None, regular=None) -> str:
    s = eur(lo) + ("起" if hi is not None and hi > lo else "")
    z = zhe(lo, regular)
    if z:
        s += f"（{z}，原价{eur(regular)}）"
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


def size_avail(detail: dict | None, sizes: list[str]) -> list[str] | None:
    """想要的尺码在哪些颜色有货。

    返回 None：没抓详情页，或这款根本没有这个尺码（鞋、帽子、背包）——不适用；
    返回 []：有这个尺码但全卖空了。
    """
    if detail is None or not sizes or not detail["has_sizes"]:
        return None
    rel = [v for v in detail["variants"] if size_matches(v["size"], sizes)]
    if not rel:
        return None
    return list(dict.fromkeys(v["colour"] for v in rel if v["status"] in ("InStock", "LowStock")))


def size_note(avail: list[str] | None, sizes: list[str]) -> str:
    if avail is None or not sizes:
        return ""
    label = "/".join(sizes) + "码"
    return f"{label}有货：{'、'.join(avail)}" if avail else f"{label}无货"


def feed_line(ev: dict, avail: list[str] | None = None, sizes: list[str] | None = None) -> str:
    t = ev["type"]
    if t == "new_product":
        head = f"🆕 {ev['name']} {price_text(ev['min'], ev['max'], ev['price'])}"
    elif t == "back_in_catalog":
        head = f"↩️ {ev['name']} 重新上架 {price_text(ev['min'], ev['max'], ev['price'])}"
    elif t == "price_drop":
        z = zhe(ev["min"], ev["price"])
        head = f"📉 {ev['name']} {eur(ev['old_min'])}→{eur(ev['min'])}" + (f"（{z}，原价{eur(ev['price'])}）" if z else "")
    elif t == "new_colour":
        head = f"🎨 {ev['name']} 新配色：{'、'.join(ev['new_colours'])}"
    else:
        head = f"{t} {ev.get('name')}"
    extra = []
    if t in ("new_product", "back_in_catalog") and ev.get("colours"):
        extra.append(f"颜色：{'、'.join(ev['colours'])}")
    note = size_note(avail, sizes or [])
    if note:
        extra.append(note)
    return head + ("\n    " + "｜".join(extra) if extra else "")


def feed_pushes(events: list[dict], country: str, language: str, more_hint: str,
                sizes: list[str] | None = None, details: dict | None = None) -> list[Push]:
    """每个站合并成一条推送。sizes + details 用来标注想要的尺码哪些颜色有货，有货的排前面。"""
    details = details or {}
    sizes = sizes or []
    out = []
    by_site: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        by_site[ev["site"]].append(ev)
    for site, evs in by_site.items():
        def avail_of(e):
            key = (e["site"], e["slug"])
            return size_avail(details[key], sizes) if key in details else None

        def rank(e):
            av = avail_of(e)
            has = 0 if (av is None or av) else 1          # 想要的尺码无货的排后面
            ratio = (e.get("min") or 0) / e["price"] if e.get("price") else 1
            return (_FEED_ORDER.get(e["type"], 9), has, ratio)
        evs.sort(key=rank)
        counts: dict[str, int] = defaultdict(int)
        for e in evs:
            counts[e["type"]] += 1
        label = SITES[site]["label"]
        title = f"{label}：" + " · ".join(
            f"{_FEED_NAME[t]} {n}" for t, n in sorted(counts.items(), key=lambda kv: _FEED_ORDER[kv[0]]))
        lines = [feed_line(e, avail_of(e), sizes) for e in evs[:MAX_FEED_ITEMS]]
        if len(evs) > MAX_FEED_ITEMS:
            lines.append(f"…还有 {len(evs) - MAX_FEED_ITEMS} 款{more_hint}")
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


def promo_push(ev: dict, products: dict, country: str, language: str, sub_feed,
               details: dict) -> Push:
    """全场折上折开始 / 结束。products: 该站当前商品快照。"""
    site, label = ev["site"], SITES[ev["site"]]["label"]
    base = f"https://{SITES[site]['host']}/{country}/{language}"
    g = sub_feed.gender
    url = f"{base}/c/{g}" if g else f"{base}/"
    z = f"{round(ev['ratio'] * 10, 1):g}折"
    if ev["type"] == "promo_end":
        return Push(title=f"{label}折上折结束了", body=f"{ev['count']}/{ev['total']} 款恢复原价位（之前约再打 {z}）。",
                    url=url, group="始祖鸟·活动")
    sizes = sub_feed.sizes
    cands = []
    for pid in ev["pids"]:
        p = products.get(pid)
        if p and sub_feed.passes_filters({**p, "type": "promo_item"}):
            cands.append(p)
    cands.sort(key=lambda p: (p.get("min") or 0) / p["price"] if p.get("price") else 1)
    who = {"mens": "男款", "womens": "女款"}.get(g, "")
    lines = [f"{ev['count']}/{ev['total']} 款降价，约在原折扣价上再打 {z}。"]
    rows = []
    for i, p in enumerate(cands):
        key = (site, p["slug"])
        av = size_avail(details.get(key), sizes) if key in details else None
        if sizes and av == []:
            continue                       # 有这个尺码但已卖空的不列
        line = f"• {p['name']} {price_text(p['min'], p['max'], p['price'])}" + (
            f"\n    {size_note(av, sizes)}" if av else "")
        rows.append((0 if av else 1, i, line))   # 想要的尺码有货的排前面，鞋帽等不分尺码的排后面
    shown = [r[2] for r in sorted(rows)[:8]]
    if shown:
        lines.append(f"{who}折扣最大" + (f"（{'/'.join(sizes)}码有货）" if sizes else "") + "：")
        lines += shown
    if len(cands) > len(shown):
        lines.append(f"…{who}共 {len(cands)} 款参与")
    return Push(title=f"🔥 {label}折上折开始了：约再打 {z}", body=_clip("\n".join(lines)), url=url,
                group="始祖鸟·活动", level="timeSensitive")


def banner_push(ev: dict, country: str, language: str) -> Push:
    label = SITES[ev["site"]]["label"]
    return Push(title=f"📣 {label}首页活动更新", body=_clip(f"现在：{ev['new']}\n之前：{ev['old']}"),
                url=f"https://{SITES[ev['site']]['host']}/{country}/{language}/", group="始祖鸟·活动")


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
