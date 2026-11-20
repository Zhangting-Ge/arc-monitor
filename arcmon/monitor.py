"""核心逻辑：抓全站列表 → 和上次对比 → 抓关注商品的尺码库存 → 按订阅人推送。"""
from __future__ import annotations

import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .config import SITES, Config, discount_pct
from .fetch import AVAILABLE, BlockedError, FetchError, SiteClient
from .notify import (Bark, Push, banner_push, eur, feed_pushes, initial_push, promo_push, sizes_text,
                     watch_pushes, zhe)
from .state import State

log = logging.getLogger(__name__)

FAIL_ALERT_AFTER = 3        # 连续失败几次才提醒（避免偶发网络抖动刷屏）
ANOMALY_HOLD_RUNS = 2       # 商品数量剧变时先忽略几次，持续如此才接受为新基线
DETAIL_TIME_BUDGET = 300    # 每次运行抓详情页最多花几秒（GitHub job 上限 10 分钟）
DETAIL_MAX_CONSEC_ERR = 3   # 同一站点详情页连续出错几次就停止本次抓取
LOW_PRICE_MEMORY_DAYS = 60  # 「新低价」的记忆期
PROMO_SHARE = 0.3           # 多少比例的商品同时降价/涨价算「全场活动」开始/结束
PROMO_MIN_COUNT = 10
PROMO_WINDOW_HOURS = 3      # 活动改价可能分几轮生效，在这个时间窗内累计
BANNER_SITES = ("outlet",)  # 监控首页横幅的站点
# 查尺码时服装优先：这些通常没有 S/M/L 尺码
NON_APPAREL = re.compile(r"\b(shoe|boot|hat|cap|toque|beanie|glove|mitten|sock|backpack|pack|bag|belt|"
                         r"bottle|sling|tote|duffel|duffle|headband|neck gaiter)s?\b", re.I)

try:
    from zoneinfo import ZoneInfo
    BERLIN = ZoneInfo("Europe/Berlin")
except Exception:  # noqa: BLE001
    BERLIN = timezone.utc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ev(now: datetime, type_: str, site: str, p: dict, **extra) -> dict:
    return {"t": now.isoformat(timespec="seconds"), "type": type_, "site": site, "id": p.get("id"),
            "name": p.get("name"), "slug": p.get("slug"), "gender": p.get("gender"), "img": p.get("img"),
            "price": p.get("price"), "min": p.get("min"), "max": p.get("max"),
            "colours": p.get("colours"), **extra}


# ======================= 全站目录对比 =======================
def _fresh(p: dict, today: str) -> dict:
    p["since"] = today
    p["seen"] = list(p["colours"])
    p["low"], p["low_d"] = p.get("min"), today
    return p


def diff_catalog(site: str, prev: dict | None, cur: dict[str, dict], now: datetime):
    """返回 (事件列表, 备注, 新快照)。备注: baseline / anomaly / ''。

    - 列表里少了的商品先标记 miss=1，连续两次都不在才算下架（防止接口偶发漏数据导致误报）
    - 新配色：只有从没见过的颜色才算（售罄颜色从列表消失又回来不算）
    - 降价：只有低于近 60 天最低价才推（避免价格来回跳反复推送）
    """
    today = now.astimezone(BERLIN).date().isoformat()
    if prev is None:
        return [], "baseline", {"products": {k: _fresh(p, today) for k, p in cur.items()}, "removed": {}}

    old: dict = prev.get("products") or {}
    confirmed_old = {k for k, v in old.items() if not v.get("miss")}
    added = cur.keys() - old.keys()
    gone = confirmed_old - cur.keys()
    if len(confirmed_old) >= 20 and (len(gone) > 0.4 * len(confirmed_old)
                                     or len(added) > 0.6 * len(confirmed_old)):
        return [], "anomaly", None

    removed: dict = dict(prev.get("removed") or {})
    products: dict = {}
    events = []
    low_cutoff = (now - timedelta(days=LOW_PRICE_MEMORY_DAYS)).date().isoformat()

    for pid in sorted(cur):
        n = cur[pid]
        o = old.get(pid)
        if o is None:
            if pid in removed:
                back = removed.pop(pid)
                _fresh(n, today)
                n["since"] = back.get("since", today)
                n["seen"] = sorted(set(back.get("seen") or []) | set(n["colours"]))
                events.append(_ev(now, "back_in_catalog", site, n, gone_since=back.get("removed")))
            else:
                _fresh(n, today)
                events.append(_ev(now, "new_product", site, n))
            products[pid] = n
            continue
        n["since"] = o.get("since", today)
        seen = set(o.get("seen") or o.get("colours") or [])
        new_cols = sorted(set(n["colours"]) - seen)
        if new_cols and seen:
            events.append(_ev(now, "new_colour", site, n, new_colours=new_cols))
        n["seen"] = sorted(seen | set(n["colours"]))
        low, low_d = o.get("low", o.get("min")), o.get("low_d", today)
        if low_d < low_cutoff:
            low, low_d = o.get("min"), today
        n["low"], n["low_d"] = low, low_d
        if n.get("min") is not None and low is not None and n["min"] < low - 0.005:
            if o.get("min") is not None and n["min"] < o["min"] - 0.005:
                events.append(_ev(now, "price_drop", site, n, old_min=o["min"]))
            n["low"], n["low_d"] = n["min"], today
        products[pid] = n

    for pid in sorted(old.keys() - cur.keys()):
        o = old[pid]
        if o.get("miss"):
            removed[pid] = {k: v for k, v in o.items() if k != "miss"} | {"removed": today}
            events.append(_ev(now, "removed", site, o))
        else:
            products[pid] = {**o, "miss": 1}

    cutoff = (now - timedelta(days=90)).date().isoformat()
    removed = {k: v for k, v in removed.items() if v.get("removed", "") >= cutoff}
    return events, "", {"products": products, "removed": removed}


# ======================= 全场活动（折上折）=======================
def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def detect_promo(site: str, old: dict, cur: dict, meta: dict, now: datetime) -> list[dict]:
    """同一时段大量商品同时降价 → 全场活动开始；大量同时涨价 → 结束。

    改价可能分几轮才全部生效，所以在 PROMO_WINDOW_HOURS 内累计。只在有价格变化时才写状态。
    """
    common = [k for k, o in old.items() if not o.get("miss") and k in cur and o.get("min") and cur[k].get("min")]
    if len(common) < 20:
        return []
    down = {k: round(cur[k]["min"] / old[k]["min"], 3) for k in common if cur[k]["min"] < old[k]["min"] * 0.97}
    up = {k: round(cur[k]["min"] / old[k]["min"], 3) for k in common if cur[k]["min"] > old[k]["min"] * 1.03}
    acc = (meta.get("promo_acc") or {}).get(site)
    cutoff = (now - timedelta(hours=PROMO_WINDOW_HOURS)).isoformat(timespec="seconds")
    if acc and acc.get("since", "") < cutoff:
        acc = None
    if not acc and not down and not up:
        if site in (meta.get("promo_acc") or {}):
            del meta["promo_acc"][site]
        return []                      # 没有价格变化：不碰状态，免得产生提交
    promos = meta.setdefault("promo", {})
    st = promos.get(site) or {"active": False}
    acc_all = meta.setdefault("promo_acc", {})
    acc = acc or {"since": now.isoformat(timespec="seconds"), "down": {}, "up": {}}
    for k, r in down.items():
        acc["down"][k] = round(acc["down"].get(k, 1) * r, 3)
        acc["up"].pop(k, None)
    for k, r in up.items():
        acc["up"][k] = round(acc["up"].get(k, 1) * r, 3)
        acc["down"].pop(k, None)
    need = max(PROMO_MIN_COUNT, PROMO_SHARE * len(common))
    t = now.isoformat(timespec="seconds")
    events = []
    if not st.get("active") and len(acc["down"]) >= need:
        ratio = _median(list(acc["down"].values()))
        st = {"active": True, "ratio": ratio, "since": now.astimezone(BERLIN).date().isoformat()}
        events.append({"t": t, "type": "promo_start", "site": site, "ratio": ratio,
                       "count": len(acc["down"]), "total": len(common), "pids": sorted(acc["down"])})
        acc = None
    elif st.get("active") and len(acc["up"]) >= need:
        events.append({"t": t, "type": "promo_end", "site": site, "ratio": st.get("ratio", 1),
                       "count": len(acc["up"]), "total": len(common)})
        st = {"active": False}
        acc = None
    promos[site] = st
    if acc:
        acc_all[site] = acc
    else:
        acc_all.pop(site, None)
    return events


def check_banner(client: SiteClient, meta: dict, now: datetime) -> list[dict]:
    try:
        text = client.home_banner()
    except Exception as e:  # noqa: BLE001  横幅只是辅助信息，失败不影响主流程
        log.warning("%s 首页横幅读取失败：%r", client.site, e)
        return []
    if not text:
        return []
    banners = meta.setdefault("banner", {})
    old = banners.get(client.site)
    banners[client.site] = text
    if old is None or old == text:
        return []
    return [{"t": now.isoformat(timespec="seconds"), "type": "banner_change", "site": client.site,
             "old": old, "new": text}]


# ======================= 关注规则 =======================
def _plan_details(cfg: Config, state: State, catalogs: dict, new_ids: set, now: datetime,
                  budget: int, done: set):
    """确定每条规则的候选商品，以及本次要抓哪些详情页（有预算上限，超出的轮流抓）。"""
    rule_targets = {}
    priority, rest = [], []
    seen: set = set()
    n_url = 0
    for sub in cfg.subscribers:
        for rule in sub.watch:
            rkey = f"{sub.name}|{rule.rid}"
            items = (state.watch.get(rkey) or {}).get("items") or {}
            if rule.slug:
                targets = [(s, rule.slug) for s in rule.sites if s in cfg.sites]
                n_url += len(targets)
            else:
                targets = []
                for site in rule.sites:
                    cat = catalogs.get(site)
                    if cat is not None:
                        targets += [(site, p["slug"]) for p in cat.values() if rule.matches_listing(site, p)]
            rule_targets[rkey] = (sub, rule, targets)
            for site, slug in targets:
                key = (site, slug)
                if key in done:
                    continue
                urgent = bool(rule.slug) or f"{site}:{slug}" not in items or key in new_ids
                if key in seen:
                    if urgent and key in rest:
                        rest.remove(key)
                        priority.append(key)
                    continue
                seen.add(key)
                (priority if urgent else rest).append(key)
    budget = max(0, budget)
    if n_url >= budget:
        log.warning("商品链接规则有 %d 条，已占满 max_detail_pages=%d，关键词规则的商品检查不到", n_url, budget)
    rest.sort()
    room = max(0, budget - len(priority))
    if rest and room < len(rest):
        # 每次运行起点前移 room 个，保证轮一圈能覆盖全部（不写状态文件，免得每次都产生提交）
        slot = int(os.environ.get("GITHUB_RUN_NUMBER") or now.timestamp() // 600)
        cursor = (slot * room) % len(rest)
        rest = rest[cursor:] + rest[:cursor]
    return rule_targets, priority[:budget] + rest[:room]


def _fetch_details(plan, clients, catalogs, failures, deadline):
    fetched: dict = {}
    listed = {s: {p["slug"] for p in c.values()} for s, c in catalogs.items() if c is not None}
    tries, errs, consec = defaultdict(int), defaultdict(int), defaultdict(int)
    stopped: set = set()
    for site, slug in plan:
        if site in stopped or site not in clients:
            continue
        if time.monotonic() > deadline:
            log.warning("详情页抓取超出时间预算，剩余的下次再查")
            break
        tries[site] += 1
        try:
            d = clients[site].detail(slug)
        except BlockedError as e:
            stopped.add(site)
            failures.setdefault(site, f"详情页：{e}")
            continue
        except Exception as e:  # noqa: BLE001  任何异常都不能让整次运行崩掉
            errs[site] += 1
            consec[site] += 1
            log.warning("详情页失败 %s %s: %r", site, slug, e)
            if consec[site] >= DETAIL_MAX_CONSEC_ERR:
                stopped.add(site)
            continue
        consec[site] = 0
        if d is None and slug in listed.get(site, ()):
            # 列表里还在卖，详情却 404：多半是临时问题，保持上次状态，别当成无货
            errs[site] += 1
            continue
        fetched[(site, slug)] = d
    for site, n in tries.items():
        if errs[site] >= 3 and errs[site] * 2 >= n:
            failures.setdefault(site, f"详情页大量失败（{errs[site]}/{n}）")
    return fetched


def process_watch(cfg: Config, state: State, clients: dict, catalogs: dict,
                  new_ids: set, now: datetime, failures: dict, deadline: float,
                  prefetched: dict | None = None, budget: int | None = None):
    prefetched = prefetched or {}
    budget = cfg.max_detail_pages if budget is None else budget
    rule_targets, plan = _plan_details(cfg, state, catalogs, new_ids, now, budget, set(prefetched))
    fetched = {**prefetched, **_fetch_details(plan, clients, catalogs, failures, deadline)}

    events, initial = [], defaultdict(list)
    t = now.isoformat(timespec="seconds")
    for rkey, (sub, rule, targets) in rule_targets.items():
        entry = state.watch.get(rkey) or {}
        items = dict(entry.get("items") or {})
        ready_before = set(entry.get("ready_sites") or [])
        alive = set()
        for site, slug in targets:
            ekey = f"{site}:{slug}"
            alive.add(ekey)
            if (site, slug) not in fetched:
                continue  # 本次没轮到 / 抓取失败，保持原状态
            d = fetched[(site, slug)]
            prev = items.get(ekey)
            if d is None:
                cur = {"site": site, "slug": slug, "name": (prev or {}).get("name", slug),
                       "img": (prev or {}).get("img"), "a": [], "low": [], "p": None, "r": None}
            else:
                wanted = [v for v in d["variants"] if rule.wants_variant(v, d["has_sizes"])]
                av = [v for v in wanted if v["status"] in AVAILABLE]
                cur = {
                    "site": site, "slug": slug, "name": d["name"], "img": d["img"],
                    "a": sorted({f"{v['colour']}|{v['size']}" for v in av}),
                    "low": sorted({f"{v['colour']}|{v['size']}" for v in av if v["status"] == "LowStock"}),
                    "p": min((v["price"] for v in av if v["price"] is not None), default=None),
                    "r": max((v["regular"] for v in av if v["regular"] is not None), default=None),
                }
            items[ekey] = cur
            base = dict(site=site, slug=slug, name=cur["name"], img=cur["img"], rule=rule.name,
                        sub=sub.name, url=clients[site].product_url(slug), price=cur["p"],
                        regular=cur["r"], avail=cur["a"], low=bool(cur["low"]))
            if prev is None:
                # 规则在这个站刚建立时静默记录；之后新出现的符合商品才提醒
                if site in ready_before and cur["a"]:
                    events.append({"t": t, "type": "watch_match", **base})
                continue
            gained = sorted(set(cur["a"]) - set(prev.get("a") or []))
            if gained:
                events.append({"t": t, "type": "watch_restock", "gained": gained, **base,
                               "low": any(g in cur["low"] for g in gained)})
            elif cur["a"] and prev.get("p") and cur["p"] and cur["p"] < prev["p"] - 0.005:
                events.append({"t": t, "type": "watch_price", "old_price": prev["p"], **base})
            if prev.get("a") and not cur["a"]:
                events.append({"t": t, "type": "watch_soldout", **base})

        # 确认下架 / 不再符合条件的移除；以后重新出现会按「新上架符合条件」提醒
        for ekey in list(items):
            site = ekey.split(":", 1)[0]
            if ekey not in alive and catalogs.get(site) is not None:
                del items[ekey]

        ready_now = set(ready_before)
        for s in rule.sites:
            if s not in cfg.sites or (not rule.slug and catalogs.get(s) is None):
                continue
            if all(f"{s}:{g}" in items for ss, g in targets if ss == s):
                ready_now.add(s)
        announced = bool(entry.get("announced"))
        if not announced and ready_now:
            initial[sub.name].append((rule.name, [e for e in items.values() if e["a"]]))
            announced = True
        state.watch[rkey] = {"sub": sub.name, "rule": rule.name, "ready_sites": sorted(ready_now),
                             "announced": announced, "items": items}

    for k in list(state.watch):
        if k not in rule_targets:
            del state.watch[k]
    return events, initial


# ======================= 主流程 =======================
def run(cfg: Config, state: State, http, bark: Bark, now: datetime | None = None) -> dict:
    now = now or _now()
    started = time.monotonic()
    clients = {s: SiteClient(http, s, cfg.country, cfg.language,
                             build_id=(state.meta.get("build_id") or {}).get(s)) for s in cfg.sites}
    catalogs: dict[str, dict | None] = {}
    events: list[dict] = []
    failures: dict[str, str] = {}
    sys_msgs: list[str] = []
    baseline_sites = []
    new_ids = set()
    anomaly = state.meta.setdefault("anomaly", {})

    for site in cfg.sites:
        label = SITES[site]["label"]
        try:
            cur = clients[site].catalog(cfg.categories)
        except Exception as e:  # noqa: BLE001
            log.error("%s 列表抓取失败：%r", site, e, exc_info=not isinstance(e, FetchError))
            failures[site] = str(e) if isinstance(e, FetchError) else f"{type(e).__name__}: {e}"
            catalogs[site] = None
            continue
        prev = state.get_catalog(site)
        evs, note, snap = diff_catalog(site, prev, cur, now)
        if note == "anomaly":
            anomaly[site] = anomaly.get(site, 0) + 1
            if anomaly[site] <= ANOMALY_HOLD_RUNS:
                log.warning("%s 商品变化异常大，本次忽略（第 %d 次）", site, anomaly[site])
                catalogs[site] = prev["products"]   # 关注规则继续用旧快照，不误删
                continue
            # 持续异常 → 接受为新基线，关注规则在这个站重新静默初始化
            _, _, snap = diff_catalog(site, None, cur, now)
            for w in state.watch.values():
                w["ready_sites"] = [s for s in w.get("ready_sites") or [] if s != site]
            sys_msgs.append(f"⚠️ {label}商品列表变化异常大且持续，已重新建立基线（本次不推送上新/下架）")
            evs = []
        anomaly.pop(site, None)
        state.catalog[site] = snap
        catalogs[site] = snap["products"]
        if note == "baseline":
            baseline_sites.append(f"{label} {len(cur)} 款")
        elif not note:
            evs += detect_promo(site, prev["products"], snap["products"], state.meta, now)
        events += evs
        new_ids |= {(site, e["slug"]) for e in evs if e["type"] in ("new_product", "back_in_catalog")}

    # ----- 首页活动横幅（有人订阅才抓） -----
    for site in BANNER_SITES:
        if site in clients and catalogs.get(site) is not None and any(
                site in s.feed.banner for s in cfg.subscribers):
            events += check_banner(clients[site], state.meta, now)

    # ----- 给全站动态补充尺码库存（feed 里设置了 sizes 的人才需要） -----
    deadline = started + DETAIL_TIME_BUDGET
    promo_sites = {e["site"] for e in events if e["type"] == "promo_start"}
    feed_keys = []
    for sub in cfg.subscribers:
        if not sub.feed.sizes:
            continue
        for e in events:
            if (e["type"] in ("new_product", "back_in_catalog", "price_drop") and sub.feed.wants(e)
                    and not (e["type"] == "price_drop" and e["site"] in promo_sites)):
                feed_keys.append((e["site"], e["slug"]))
        for e in events:
            if e["type"] == "promo_start" and e["site"] in sub.feed.promo:
                prods = catalogs[e["site"]]
                cands = [prods[k] for k in e["pids"] if k in prods and sub.feed.passes_filters(prods[k])]
                cands.sort(key=lambda p: (bool(NON_APPAREL.search(p["name"])),
                                          (p.get("min") or 0) / p["price"] if p.get("price") else 1))
                feed_keys += [(e["site"], p["slug"]) for p in cands[:30]]
    feed_keys = list(dict.fromkeys(feed_keys))[:max(0, cfg.max_detail_pages)]
    feed_details = _fetch_details(feed_keys, clients, catalogs, failures, deadline) if feed_keys else {}

    try:
        watch_events, initial = process_watch(cfg, state, clients, catalogs, new_ids, now, failures,
                                              deadline=deadline, prefetched=feed_details,
                                              budget=cfg.max_detail_pages - len(feed_keys))
    except Exception as e:  # noqa: BLE001
        log.exception("关注规则处理出错")
        watch_events, initial = [], {}
        sys_msgs.append(f"⚠️ 关注清单处理出错：{type(e).__name__}: {e}")
    events += watch_events

    # ----- 失败计数 / 拦截提醒（计数封顶，避免故障期间每次运行都改状态文件） -----
    fails = state.meta.setdefault("fail", {})
    alerted = state.meta.setdefault("fail_alerted", {})
    for site in cfg.sites:
        label = SITES[site]["label"]
        if site in failures:
            fails[site] = min(fails.get(site, 0) + 1, FAIL_ALERT_AFTER)
            if fails[site] >= FAIL_ALERT_AFTER and not alerted.get(site):
                alerted[site] = True
                sys_msgs.append(f"⚠️ {label}已连续 {FAIL_ALERT_AFTER} 次抓取失败：{failures[site]}\n"
                                "如果一直这样，多半是 GitHub 机房 IP 被网站拦了，见 README「被拦截怎么办」")
        else:
            if alerted.get(site):
                sys_msgs.append(f"✅ {label}抓取已恢复正常")
            fails.pop(site, None)
            alerted.pop(site, None)
    state.meta["build_id"] = {s: c.build_id for s, c in clients.items() if c.build_id}

    # ----- 推送 -----
    if baseline_sites:
        msg = "✅ 监控已启动：" + "、".join(baseline_sites) + "\n已记录当前状态，之后有上新/降价/补货才会推送。"
        for sub in cfg.subscribers:
            bark.send(sub, Push(title="始祖鸟监控已启动", body=msg))
    for sub in cfg.admins:
        for m in sys_msgs:
            bark.send(sub, Push(title="始祖鸟监控 · 系统", body=m, level="passive"))

    for sub in cfg.subscribers:
        if initial.get(sub.name):
            bark.send(sub, initial_push(initial[sub.name]))
        mine = [e for e in watch_events if e["sub"] == sub.name and e["type"] != "watch_soldout"]
        for p in watch_pushes(mine):
            bark.send(sub, p)
        watched = {(e["site"], e["slug"]) for e in mine}
        wanted = [e for e in events if sub.feed.wants(e) and (e["site"], e.get("slug")) not in watched]
        my_promo_sites = {e["site"] for e in wanted if e["type"] == "promo_start"}
        for e in wanted:
            if e["type"] in ("promo_start", "promo_end"):
                bark.send(sub, promo_push(e, catalogs.get(e["site"]) or {}, cfg.country, cfg.language,
                                          sub.feed, feed_details))
            elif e["type"] == "banner_change":
                bark.send(sub, banner_push(e, cfg.country, cfg.language))
        feed = [e for e in wanted if e["type"] in ("new_product", "back_in_catalog", "price_drop", "new_colour")
                and not (e["type"] == "price_drop" and e["site"] in my_promo_sites)]   # 折上折时降价已在活动推送里
        hint = "，今晚汇总里看" if sub.digest else ""
        for p in feed_pushes(feed, cfg.country, cfg.language, hint, sizes=sub.feed.sizes, details=feed_details):
            bark.send(sub, p)

    state.log_events(events)
    changed = state.save()
    summary = {
        "sites": {s: (len(c) if c is not None else None) for s, c in catalogs.items()},
        "events": len(events), "failures": failures, "changed_files": changed,
        "pushes": len(bark.sent), "seconds": round(time.monotonic() - started, 1),
    }
    log.info("完成：%s", summary)
    return summary


# ======================= 每日汇总 =======================
def digest(cfg: Config, state: State, bark: Bark, now: datetime | None = None) -> int:
    now = now or _now()
    events = state.read_events(now - timedelta(hours=24))
    local = now.astimezone(BERLIN)
    names = {"new_product": "上新", "new_colour": "新配色", "price_drop": "降价",
             "back_in_catalog": "重新上架", "removed": "下架"}
    count = 0
    for sub in cfg.subscribers:
        if not sub.digest:
            continue
        mine = [e for e in events if e["type"] in names and sub.feed.passes_filters(e)]
        blocks = []
        for site in cfg.sites:
            evs = [e for e in mine if e["site"] == site]
            c = defaultdict(int)
            for e in evs:
                c[e["type"]] += 1
            parts = [f"{names[t]} {c[t]}" for t in names if c[t]]
            blocks.append(f"{SITES[site]['label']}：" + (" · ".join(parts) if parts else "无变化"))
            pr = (state.meta.get("promo") or {}).get(site) or {}
            if pr.get("active"):
                blocks.append(f"🔥 {SITES[site]['label']}折上折进行中（约再打 {round(pr['ratio'] * 10, 1):g}折，"
                              f"{pr.get('since', '')} 开始）")

        hi = [e for e in mine if e["type"] in ("new_product", "price_drop", "back_in_catalog")]
        hi.sort(key=lambda e: -discount_pct(e.get("min"), e.get("price")))
        if hi:
            blocks.append("—— 今日值得看 ——")
            for e in hi[:6]:
                z = zhe(e.get("min"), e.get("price"))
                blocks.append(f"• [{SITES[e['site']]['label']}{names[e['type']]}] {e['name']} {eur(e['min'])}"
                              + (f" {z}" if z else ""))

        if sub.watch:
            blocks.append("—— 你的关注 ——")
            hits = [e for e in events if e.get("sub") == sub.name and e["type"].startswith("watch_")
                    and e["type"] != "watch_soldout"]
            if hits:
                blocks.append(f"今天共提醒 {len(hits)} 次")
            for rule in sub.watch:
                items = (state.watch.get(f"{sub.name}|{rule.rid}") or {}).get("items") or {}
                have = sorted((i for i in items.values() if i.get("a")), key=lambda i: i.get("p") or 0)
                if not have:
                    blocks.append(f"【{rule.name}】暂无货")
                    continue
                first = have[0]
                more = f" 等 {len(have)} 款" if len(have) > 1 else ""
                blocks.append(f"【{rule.name}】{first['name']} {sizes_text(first['a'])} {eur(first['p'])}{more}")

        site0 = "outlet" if "outlet" in cfg.sites else cfg.sites[0]
        bark.send(sub, Push(
            title=f"始祖鸟日报 {local:%m-%d}",
            body="\n".join(blocks)[:1800],
            url=f"https://{SITES[site0]['host']}/{cfg.country}/{cfg.language}/c/new-arrivals",
            group="始祖鸟·日报",
        ))
        count += 1
    return count


# ======================= 连通性测试 =======================
def selftest(cfg: Config, http, bark: Bark) -> bool:
    lines, ok_all = [f"抓取方式：{getattr(http, 'engine', '?')}"], True
    for site in cfg.sites:
        c = SiteClient(http, site, cfg.country, cfg.language)
        label = SITES[site]["label"]
        try:
            d = c.listing_page(cfg.categories[0], 0, 5)
            total = (d.get("pagination") or {}).get("totalCount")
            first = (d.get("productList") or [{}])[0]
            line = f"✅ {label} 列表：{cfg.categories[0]} 共 {total} 款"
            det = c.detail(first["slug"]) if first.get("slug") else None
            if det:
                n_av = sum(v["status"] in AVAILABLE for v in det["variants"])
                line += f"；详情页 ✓（{det['name']}：{n_av}/{len(det['variants'])} 个颜色尺码有货）"
            else:
                line += "；详情页 ✗"
                ok_all = False
        except Exception as e:  # noqa: BLE001
            line = f"❌ {label}：{e}"
            ok_all = False
        lines.append(line)
    body = "\n".join(lines)
    print(body)
    for sub in cfg.subscribers:
        bark.send(sub, Push(title="🧪 始祖鸟监控 · 连接测试", body=body))
    return ok_all
