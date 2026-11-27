"""用真实抓到的接口数据（tests/fixtures/capture.json）模拟多次运行，检查对比和推送逻辑。

运行：python -m pytest tests -q   （或 python tests/test_monitor.py）
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arcmon.config import load_config  # noqa: E402
from arcmon.monitor import digest, run  # noqa: E402
from arcmon.notify import Bark  # noqa: E402
from arcmon.state import State  # noqa: E402

FIX = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "capture.json"), encoding="utf-8"))
BUILD = FIX["buildId"]
SIZES = ["XS", "S", "M", "L", "XL"]


class FakeSite:
    """把抓到的真实数据当成"网站"，可以随意修改来模拟上新、降价、补货。"""

    def __init__(self):
        self.plp = {}  # host|cat -> list of raw items
        for k, pages in FIX["plp"].items():
            self.plp[k] = [copy.deepcopy(x) for p in pages for x in p["productList"]]
        self.pdp_raw = {k: json.loads(v["pageProps"]["product"]) for k, v in FIX["pdp"].items()}
        self.stock_override = {}  # (host, slug, colour, size) -> status
        self.blocked = set()
        self.requests = []
        self.banner = "Just landed"

    # ---- 模拟修改 ----
    def find(self, host, slug):
        for k, items in self.plp.items():
            if k.startswith(host + "|"):
                for x in items:
                    if x["slug"] == slug:
                        return x
        raise KeyError(slug)

    def remove(self, host, slug):
        for k in self.plp:
            if k.startswith(host + "|"):
                self.plp[k] = [x for x in self.plp[k] if x["slug"] != slug]

    def add_copy(self, host, cat, src_slug, new_id, new_slug, name, price=None):
        x = copy.deepcopy(self.find(host, src_slug))
        x.update(id=new_id, slug=new_slug, marketingName=name, analyticsName=name)
        if price:
            x["priceRange"].update(minDiscountPrice=price, maxDiscountPrice=price)
        self.plp[f"{host}|{cat}/"].insert(0, x)

    def set_price(self, host, slug, price):
        for k, items in self.plp.items():
            if k.startswith(host + "|"):
                for x in items:
                    if x["slug"] == slug:
                        x["priceRange"].update(minDiscountPrice=price, maxDiscountPrice=price)

    # ---- 模拟 HTTP ----
    def _product(self, host, slug):
        key = f"{host}|{slug}"
        if key in self.pdp_raw:
            p = copy.deepcopy(self.pdp_raw[key])
        else:
            x = self.find(host, slug)
            cols = [c["label"] for c in x["colourOptions"]]
            pr = x["priceRange"]
            name = x["marketingName"]
            if any(w in name for w in ("Shoe", "Boot")):
                sizes = ["41 1/3", "42", "42 2/3", "43 1/3"]          # 鞋码
            elif any(w in name for w in ("Hat", "Cap", "Toque", "Beanie", "Glove")):
                sizes = ["SM", "LXL"]
            elif any(w in name for w in ("Backpack", "Pack", "Bag")):
                sizes = []                                            # 均码
            else:
                sizes = SIZES
            p = {"id": x["id"], "marketingName": name, "slug": slug,
                 "mainImage": {"url": "https://img/x.jpg"},
                 "colourOptions": [{"id": str(i), "label": c} for i, c in enumerate(cols)],
                 "sizeOptions": {"options": [{"label": s, "value": s} for s in sizes]},
                 "variants": []}
            for i, c in enumerate(cols):
                for s in (sizes or [None]):
                    h = int(hashlib.md5(f"{slug}{c}{s}".encode()).hexdigest(), 16) % 3
                    p["variants"].append({"colourId": str(i), "sizeId": s,
                                          "stockStatus": ["InStock", "LowStock", "OutOfStock"][h],
                                          "price": pr["regularPrice"], "discountPrice": pr["minDiscountPrice"]})
        cmap = {str(c["id"]): c["label"] for c in p["colourOptions"]}
        smap = {str(o["value"]): o["label"] for o in p["sizeOptions"]["options"]}
        for v in p["variants"]:
            k = (host, slug, cmap[str(v["colourId"])], smap.get(str(v["sizeId"])) if v["sizeId"] else None)
            if k in self.stock_override:
                v["stockStatus"] = self.stock_override[k]
        return p

    def get(self, url, *, html=False, referer=None):
        self.requests.append(url)
        u = urlparse(url)
        host = u.netloc
        if host in self.blocked:
            return 403, "<html>blocked</html>"
        if u.path == "/api/catalog.getProductListingPage":
            inp = json.loads(parse_qs(u.query)["input"][0])["json"]
            items = self.plp.get(f"{host}|{inp['slug']}", [])
            off, lim = inp["offset"], inp["limit"]
            page = items[off:off + lim]
            body = {"result": {"data": {"json": {"productList": page, "pagination": {
                "hasMore": off + lim < len(items), "limit": lim, "offset": off, "totalCount": len(items)}}}}}
            return 200, json.dumps(body)
        if "/_next/data/" in u.path:
            bid = u.path.split("/_next/data/")[1].split("/")[0]
            if bid != BUILD:
                return 404, "not found"
            slug = unquote(u.path.split("/shop/", 1)[1][:-len(".json")])
            try:
                prod = self._product(host, slug)
            except KeyError:
                return 404, "not found"
            return 200, json.dumps({"pageProps": {"product": json.dumps(prod)}})
        if html and u.path.rstrip("/") == "/de/en":
            nd = {"buildId": "HOME", "props": {"pageProps": {
                "heroBanner": {"eyebrow": None, "title": self.banner, "description": None},
                "navigationData": [{"promoMessage": {"content": [{"children": [
                    {"text": "Free Shipping | Free "}, {"text": "Returns"}]}]}}]}}}
            return 200, f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></html>'
        if html:
            return 200, f'<html><script id="__NEXT_DATA__">{{"buildId":"{BUILD}"}}</script></html>'
        return 404, ""


CONFIG = """
sites: [main, outlet]
max_detail_pages: 40
subscribers:
  - name: 我
    admin: true
    feed:
      new_products: [outlet, main]
      new_colours: [main]
      price_drops: [outlet, main]
      back_in_catalog: [outlet]
      promo: [outlet, main]
      banner: [outlet]
      gender: mens
      sizes: [M]
    watch:
      - name: 奥莱 Beta 外套 M
        site: outlet
        keywords: [beta, jacket]
        gender: mens
        sizes: [M]
        max_price: 450
      - name: Gamma MX 黑色 M
        url: https://arcteryx.com/de/en/shop/mens/gamma-mx-hoody-8485
        colours: [Black]
        sizes: [M]
  - name: 朋友A
    daily_digest: false
    feed:
      new_products: [outlet]
      gender: womens
    watch:
      - name: Atom 女款 S
        site: outlet
        keywords: [atom]
        gender: womens
        sizes: [S]
"""


def _setup():
    d = tempfile.mkdtemp()
    cfg_path = os.path.join(d, "config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(CONFIG)
    os.environ.pop("MONITOR_CONFIG", None)
    return load_config(cfg_path), os.path.join(d, "state")


def _titles(bark, who=None):
    return [p.title for n, p in bark.sent if who is None or n == who]


def test_full_cycle():
    cfg, sdir = _setup()
    site = FakeSite()
    t0 = datetime(2026, 11, 20, 10, 3, tzinfo=timezone.utc)

    # ---- 第 1 次：建立基线 ----
    b1 = Bark(dry_run=True)
    s1 = run(cfg, State(sdir), site, b1, now=t0)
    assert s1["sites"] == {"main": 405, "outlet": 173}, s1
    assert s1["events"] == 0
    assert "始祖鸟监控已启动" in _titles(b1, "我") and "始祖鸟监控已启动" in _titles(b1, "朋友A")
    assert "📋 关注清单已生效" in _titles(b1, "我")
    assert "📋 关注清单已生效" in _titles(b1, "朋友A")
    assert not any("补货" in t or "上新" in t for t in _titles(b1))
    assert os.path.exists(os.path.join(sdir, "catalog_main.json"))

    # ---- 第 2 次：没变化 → 不推送、状态文件不变 ----
    b2 = Bark(dry_run=True)
    s2 = run(cfg, State(sdir), site, b2, now=t0 + timedelta(minutes=10))
    assert s2["events"] == 0 and b2.sent == [], [p.title for _, p in b2.sent]
    assert s2["changed_files"] == [], s2["changed_files"]

    # ---- 第 3 次：模拟各种变化 ----
    O, M = "outlet.arcteryx.com", "arcteryx.com"
    site.add_copy(O, "mens", "mens/beta-jacket-0511", "X999000001", "mens/beta-lt-jacket-9990",
                  "Beta LT Jacket Men's", price=320)                          # 奥莱上新，且符合 Beta 规则
    site.set_price(O, "mens/squamish-hoody-0276", 120)                        # 奥莱再降价 140 -> 120
    site.set_price(M, "mens/atom-hoody-9556", 196)                            # 正价站打折
    site.find(M, "mens/beta-jacket-0868")["colourOptions"].append(
        {"label": "Test Green", "badges": [{"code": "newcolour"}], "selected": False})  # 新配色
    site.remove(O, "mens/alpha-jacket-9898")                                  # 奥莱下架
    site.add_copy(O, "womens", "womens/atom-hoody-9452", "X999000002", "womens/atom-lt-hoody-9991",
                  "Atom LT Hoody Women's", price=150)                         # 女款上新（朋友A）
    # Gamma MX 黑色 M：先看现在什么状态，然后翻转成有货 / 无货
    gm = site._product(M, "mens/gamma-mx-hoody-8485")
    black = [c["label"] for c in gm["colourOptions"] if c["label"] == "Black"][0]
    site.stock_override[(M, "mens/gamma-mx-hoody-8485", black, "M")] = "OutOfStock"   # 先卖空

    b3 = Bark(dry_run=True)
    run(cfg, State(sdir), site, b3, now=t0 + timedelta(minutes=20))
    me = _titles(b3, "我")
    friend = _titles(b3, "朋友A")
    print("\n我 收到：", me, "\n朋友A 收到：", friend)
    types = [json.loads(ln)["type"] for ln in open(os.path.join(sdir, "events.jsonl"), encoding="utf-8")]
    assert {"new_product", "price_drop", "new_colour"} <= set(types), types
    assert "removed" not in types                      # 只少了一次，先不算下架
    # 新上架的 Beta LT 有 M 码 → 我收到「新上架符合条件」（并且不会在上新汇总里重复）
    beta_lt_avail = any(v["stockStatus"] != "OutOfStock" and v["sizeId"] == "M"
                        for v in site._product(O, "mens/beta-lt-jacket-9990")["variants"])
    if beta_lt_avail:
        assert any("新上架符合条件" in t for t in me), me
    feed_bodies = "\n".join(p.body for n, p in b3.sent if n == "我" and "：" in p.title)
    assert "Squamish Hoody Men's €140→€120" in feed_bodies, feed_bodies
    assert "Atom Hoody Men's €280→€196（7折，原价€280）" in feed_bodies, feed_bodies
    assert "Squamish Hoody Men's €140→€120（6折，原价€200）" in feed_bodies, feed_bodies
    assert "M码" in feed_bodies, feed_bodies
    assert "新配色：Test Green" in feed_bodies
    assert "Atom LT Hoody" not in feed_bodies          # 我只看男款
    assert not any("Gamma MX" in (p.subtitle or "") for n, p in b3.sent)   # 卖空只进日报，不推送
    assert any(t.startswith("奥莱：上新") for t in friend), friend
    assert not any("正价站" in t for t in friend)    # 朋友只订了奥莱

    # ---- 第 4 次：Gamma MX 补货；下架的商品连续第二次不在列表，仍不算下架 ----
    site.stock_override[(M, "mens/gamma-mx-hoody-8485", black, "M")] = "LowStock"     # 补货（少量）
    b4 = Bark(dry_run=True)
    run(cfg, State(sdir), site, b4, now=t0 + timedelta(minutes=30))
    types = [json.loads(ln)["type"] for ln in open(os.path.join(sdir, "events.jsonl"), encoding="utf-8")]
    assert "removed" not in types, types
    restock = [p for n, p in b4.sent if n == "我" and "补货了" in p.title]
    assert restock and restock[0].subtitle == "Gamma MX Hoody Men's", [p.title for _, p in b4.sent]
    assert restock[0].level == "timeSensitive" and "M（Black） 有货了（库存紧张）" in restock[0].body, restock[0].body
    assert restock[0].url == "https://arcteryx.com/de/en/shop/mens/gamma-mx-hoody-8485"

    # ---- 第 5 次：连续第三次不在列表 → 确认下架 ----
    run(cfg, State(sdir), site, Bark(dry_run=True), now=t0 + timedelta(minutes=40))
    types = [json.loads(ln)["type"] for ln in open(os.path.join(sdir, "events.jsonl"), encoding="utf-8")]
    assert "removed" in types, types

    # ---- 8 小时后：下架的商品重新上架 → 推送 ----
    alpha = copy.deepcopy(
        [x for p in FIX["plp"][f"{O}|mens/"] for x in p["productList"] if x["slug"] == "mens/alpha-jacket-9898"][0])
    site.plp[f"{O}|mens/"].append(alpha)
    T = t0 + timedelta(hours=8)
    b5 = Bark(dry_run=True)
    run(cfg, State(sdir), site, b5, now=T)
    assert any("重新上架" in p.body for n, p in b5.sent if n == "我"), [p.body for _, p in b5.sent]

    # ---- 被拦截：连续 3 次失败才提醒一次，恢复后再提醒一次；故障期间状态文件不再变化 ----
    site.blocked.add(M)
    alerts = []
    for i in range(5):
        b = Bark(dry_run=True)
        r = run(cfg, State(sdir), site, b, now=T + timedelta(minutes=10 + i * 10))
        assert "main" in r["failures"]
        alerts += [p.body for n, p in b.sent if "系统" in p.title]
        if i >= 3:
            assert r["changed_files"] == [], r["changed_files"]
    assert len(alerts) == 1 and "正价站已连续 3 次" in alerts[0], alerts
    site.blocked.clear()
    b = Bark(dry_run=True)
    run(cfg, State(sdir), site, b, now=T + timedelta(minutes=60))
    assert any("恢复正常" in p.body for _, p in b.sent)
    # 失败期间正价站没有被误判成"全部下架"
    b = Bark(dry_run=True)
    r = run(cfg, State(sdir), site, b, now=T + timedelta(minutes=70))
    assert r["events"] == 0, r

    # ---- 每日汇总 ----
    b6 = Bark(dry_run=True)
    n = digest(cfg, State(sdir), b6, now=t0 + timedelta(hours=10))
    assert n == 1  # 朋友A 关了日报
    body = b6.sent[0][1].body
    print("\n日报：\n" + body)
    assert "奥莱：" in body and "正价站：" in body and "你的关注" in body


def test_build_id_refresh():
    """网站重新部署导致 buildId 变化时，能自动拿新的。"""
    cfg, sdir = _setup()
    site = FakeSite()
    st = State(sdir)
    st.meta["build_id"] = {"main": "OLD", "outlet": "OLD"}
    st.save()
    b = Bark(dry_run=True)
    run(cfg, State(sdir), site, b, now=datetime(2026, 11, 20, tzinfo=timezone.utc))
    assert State(sdir).meta["build_id"]["outlet"] == BUILD


def test_detail_budget_rotation():
    """关注的商品超过 max_detail_pages 时分批检查，初始清单等全部检查完才发。"""
    cfg, sdir = _setup()
    cfg.max_detail_pages = 3
    site = FakeSite()
    t0 = datetime(2026, 11, 20, tzinfo=timezone.utc)
    sent_initial = []
    for i in range(8):
        b = Bark(dry_run=True)
        run(cfg, State(sdir), site, b, now=t0 + timedelta(minutes=10 * i))
        sent_initial += [n for n, p in b.sent if p.title == "📋 关注清单已生效"]
        n_details = sum("/_next/data/" in u for u in site.requests)
        site.requests.clear()
        assert n_details <= 3 + 1, n_details
    assert sent_initial.count("我") == 1 and sent_initial.count("朋友A") == 1, sent_initial
    for k, v in State(sdir).watch.items():
        assert v["ready_sites"] and v["announced"], k


def _baseline(cfg=None, sdir=None, site=None, runs=1, t0=datetime(2026, 11, 20, tzinfo=timezone.utc)):  # noqa: B008
    if cfg is None:
        cfg, sdir = _setup()
    site = site or FakeSite()
    for i in range(runs):
        run(cfg, State(sdir), site, Bark(dry_run=True), now=t0 + timedelta(minutes=10 * i))
    return cfg, sdir, site, t0 + timedelta(minutes=10 * runs)


def _pushes_after(cfg, sdir, site, t, n=1):
    out = []
    for i in range(n):
        b = Bark(dry_run=True)
        run(cfg, State(sdir), site, b, now=t + timedelta(minutes=10 * i))
        out += [(who, p) for who, p in b.sent]
    return out


def test_category_blip_no_false_push():
    """某个分类偶发返回空列表一次：不能误报下架，也不能在恢复后误报「重新上架 / 符合条件」。"""
    cfg, sdir, site, t = _baseline()
    saved = site.plp["outlet.arcteryx.com|womens/"]
    site.plp["outlet.arcteryx.com|womens/"] = []
    sent = _pushes_after(cfg, sdir, site, t)
    site.plp["outlet.arcteryx.com|womens/"] = saved
    sent += _pushes_after(cfg, sdir, site, t + timedelta(minutes=10), n=2)
    assert sent == [], [(w, p.title, p.body) for w, p in sent]


def test_anomaly_hold_then_accept():
    """一半商品突然消失：前两次忽略（关注状态不动），持续第三次才接受新基线并提醒管理员。"""
    cfg, sdir, site, t = _baseline()
    for k in list(site.plp):
        if k.startswith("outlet.arcteryx.com|"):
            site.plp[k] = site.plp[k][: len(site.plp[k]) // 4]
    w_before = json.dumps(State(sdir).watch, sort_keys=True)
    sent = _pushes_after(cfg, sdir, site, t, n=2)
    assert sent == [], [(w, p.title) for w, p in sent]
    assert json.dumps(State(sdir).watch, sort_keys=True) == w_before
    sent = _pushes_after(cfg, sdir, site, t + timedelta(minutes=20))
    assert [p.title for _, p in sent] == ["始祖鸟监控 · 系统"], [(w, p.title, p.body) for w, p in sent]


def test_detail_404_kept_not_soldout():
    """列表里还在卖但详情页 404：保持上次状态，恢复后不误报补货；持续失败要报警。"""
    cfg, sdir, site, t = _baseline()
    orig = site._product
    site._product = lambda host, slug: (_ for _ in ()).throw(KeyError(slug))   # 所有详情页 404
    sent = _pushes_after(cfg, sdir, site, t, n=3)
    assert any("详情页大量失败" in p.body for _, p in sent), [(p.title, p.body) for _, p in sent]
    site._product = orig
    sent = _pushes_after(cfg, sdir, site, t + timedelta(minutes=30))
    assert not any("补货" in p.title for _, p in sent), [(p.title, p.body) for _, p in sent]


def test_malformed_detail_no_crash():
    cfg, sdir, site, t = _baseline()
    real_get = site.get
    site.get = lambda url, **kw: (200, "{not json") if "/_next/data/" in url else real_get(url, **kw)
    r = run(cfg, State(sdir), site, Bark(dry_run=True), now=t)
    assert r["sites"]["outlet"] == 173


def test_rule_ready_per_site_when_other_site_blocked():
    """规则同时看两个站，其中一个站一直被拦：另一个站的新上架照样要提醒。"""
    cfg, sdir = _setup()
    cfg.subscribers[0].watch[0].sites = ["main", "outlet"]
    site = FakeSite()
    site.blocked.add("arcteryx.com")
    cfg, sdir, site, t = _baseline(cfg, sdir, site)
    site.add_copy("outlet.arcteryx.com", "mens", "mens/beta-jacket-0511", "X999000003",
                  "mens/beta-zz-jacket-9993", "Beta ZZ Jacket Men's", price=300)
    site.stock_override[("outlet.arcteryx.com", "mens/beta-zz-jacket-9993", "Dk Shincha", "M")] = "InStock"
    sent = _pushes_after(cfg, sdir, site, t)
    assert any("新上架符合条件" in p.title and p.subtitle == "Beta ZZ Jacket Men's" for _, p in sent), \
        [(p.title, p.subtitle) for _, p in sent]


def test_rotation_covers_everything():
    """预算不够时轮流检查，若干次运行后每个候选商品都要被检查到。"""
    cfg, sdir = _setup()
    cfg.max_detail_pages = 2
    cfg.subscribers[0].watch = cfg.subscribers[0].watch[:1]      # 只留 Beta 规则（奥莱约 4 款）
    cfg.subscribers = cfg.subscribers[:1]
    site = FakeSite()
    seen = set()
    t0 = datetime(2026, 11, 20, tzinfo=timezone.utc)
    for i in range(12):
        site.requests.clear()
        run(cfg, State(sdir), site, Bark(dry_run=True), now=t0 + timedelta(minutes=10 * i))
        seen |= {u.split("/shop/")[1] for u in site.requests if "/_next/data/" in u}
    rkey = next(iter(State(sdir).watch))
    targets = {k.split(":", 1)[1] + ".json" for k in State(sdir).watch[rkey]["items"]}
    assert targets and targets <= seen, (targets, seen)


def test_colour_flap_and_null_price():
    from arcmon.fetch import norm_detail, norm_listing
    cfg, sdir, site, t = _baseline()
    x = site.find("arcteryx.com", "mens/beta-jacket-0868")
    gone = x["colourOptions"].pop()
    _pushes_after(cfg, sdir, site, t)
    x["colourOptions"].append(gone)                     # 售罄颜色回到列表，不算新配色
    sent = _pushes_after(cfg, sdir, site, t + timedelta(minutes=10))
    assert not any("新配色" in p.body for _, p in sent), [p.body for _, p in sent]
    p = norm_listing({"id": "X", "slug": "a", "marketingName": "A",
                      "priceRange": {"regularPrice": 100, "minDiscountPrice": None, "maxDiscountPrice": None}})
    assert p["min"] == 100 and p["max"] == 100
    d = norm_detail({"pageProps": {"product": json.dumps({
        "id": "X", "slug": "a", "colourOptions": [{"id": 1, "label": "Black"}], "sizeOptions": {"options": []},
        "variants": [{"colourId": 1, "sizeId": None, "stockStatus": "InStock", "price": 80, "discountPrice": None}]})}})
    assert d["variants"][0]["price"] == 80 and d["variants"][0]["size"] == "均码"


def test_outlet_promo_extra_discount():
    """奥莱全场在折扣价上再打 85 折（可能分两轮生效）→ 一条「折上折开始」，列男款 M 码有货的；之后恢复 → 「结束」。"""
    cfg, sdir, site, t = _baseline()
    O = "outlet.arcteryx.com"
    keys = [k for k in site.plp if k.startswith(O + "|")]
    items = {x["id"]: x for k in keys for x in site.plp[k]}
    ids = sorted(items)
    orig = {i: dict(items[i]["priceRange"]) for i in ids}

    def apply(sel, f):
        for k in keys:
            for x in site.plp[k]:
                if x["id"] in sel:
                    pr = x["priceRange"]
                    pr["minDiscountPrice"] = round(orig[x["id"]]["minDiscountPrice"] * f, 2)
                    pr["maxDiscountPrice"] = round(orig[x["id"]]["maxDiscountPrice"] * f, 2)
    half = set(ids[: len(ids) // 5])
    apply(half, 0.85)                                  # 第一轮只有 20% 生效：还不算活动
    s1 = _pushes_after(cfg, sdir, site, t)
    assert not any("折上折" in p.title for _, p in s1), [p.title for _, p in s1]
    apply(set(ids), 0.85)                              # 第二轮全部生效
    s2 = _pushes_after(cfg, sdir, site, t + timedelta(minutes=10))
    promo = [p for w, p in s2 if w == "我" and "折上折开始" in p.title]
    assert len(promo) == 1, [(w, p.title) for w, p in s2]
    print("\n" + promo[0].title + "\n" + promo[0].body)
    assert "8.5折" in promo[0].title and "M码有货" in promo[0].body
    assert "Women's" not in promo[0].body
    assert promo[0].level == "timeSensitive"
    assert not any(t.startswith("奥莱：降价") for t in [p.title for w, p in s2 if w == "我"])   # 不再逐条刷降价
    # 活动期间奥莱上新：带颜色和 M 码
    site.add_copy(O, "mens", "mens/squamish-hoody-0276", "X999000009", "mens/squamish-bf-hoody-9999",
                  "Squamish BF Hoody Men's", price=120)
    s3 = _pushes_after(cfg, sdir, site, t + timedelta(minutes=20))
    feed = [p for w, p in s3 if w == "我" and p.title.startswith("奥莱：上新")]
    assert feed and "颜色：" in feed[0].body and "M码" in feed[0].body and "6折" in feed[0].body, \
        [(p.title, p.body) for _, p in s3]
    b = Bark(dry_run=True)
    digest(cfg, State(sdir), b, now=t + timedelta(minutes=30))
    assert "折上折进行中" in b.sent[0][1].body, b.sent[0][1].body
    apply(set(ids), 1.0)                               # 活动真的结束：价格持续回升
    s4 = _pushes_after(cfg, sdir, site, t + timedelta(minutes=40), n=6)
    ends = [p for _, p in s4 if "折上折结束" in p.title]
    assert len(ends) == 1, [p.title for _, p in s4]


def test_promo_flapping_like_black_friday_2026():
    """复现 11/23 的情况：活动价出现后，CDN 新旧价格来回跳。不能推「结束」再推「开始」。"""
    cfg, sdir, site, t = _baseline()
    O = "outlet.arcteryx.com"
    keys = [k for k in site.plp if k.startswith(O + "|")]
    orig = {x["id"]: dict(x["priceRange"]) for k in keys for x in site.plp[k]}

    def prices(f):
        for k in keys:
            for x in site.plp[k]:
                o = orig[x["id"]]
                x["priceRange"].update(minDiscountPrice=round(o["minDiscountPrice"] * f, 2),
                                       maxDiscountPrice=round(o["maxDiscountPrice"] * f, 2))
    seq = [0.8, 1.0, 1.0, 0.8, 0.8, 1.0, 0.8, 0.8, 0.8]      # 10 分钟一次，新旧价格来回
    titles = []
    for i, f in enumerate(seq):
        prices(f)
        titles += [p.title for w, p in _pushes_after(cfg, sdir, site, t + timedelta(minutes=10 * i)) if w == "我"]
    assert sum("折上折开始" in x for x in titles) == 1, titles
    assert not any("折上折结束" in x for x in titles), titles


def test_banner_change():
    cfg, sdir, site, t = _baseline()
    site.banner = "Black Friday: extra 15% off outlet"
    sent = _pushes_after(cfg, sdir, site, t)
    b = [p for w, p in sent if "首页活动" in p.title]
    assert b and "Black Friday" in b[0].body and "Just landed" in b[0].body, [(p.title, p.body) for _, p in sent]
    assert _pushes_after(cfg, sdir, site, t + timedelta(minutes=10)) == []    # 没再变就不重复推


def test_placeholder_price_ignored():
    """售罄商品价格变成 999999 再恢复：不能被当成「降价」推送。"""
    cfg, sdir, site, t = _baseline()
    x = site.find("outlet.arcteryx.com", "mens/squamish-hoody-0276")
    real = dict(x["priceRange"])
    x["priceRange"].update(minDiscountPrice=999999, maxDiscountPrice=999999)
    s1 = _pushes_after(cfg, sdir, site, t)
    x["priceRange"].update(real)
    s2 = _pushes_after(cfg, sdir, site, t + timedelta(minutes=10))
    assert not any("降价" in p.title for _, p in s1 + s2), [(p.title, p.body) for _, p in s1 + s2]


def test_colour_level_sale_and_new_colour_on_main():
    """某个颜色原来是原价、后来也打折（最低价不变）要推；正价站新配色要带价格。"""
    M = "arcteryx.com"
    site = FakeSite()
    items = [x for x in site.plp[f"{M}|mens/"]]
    mixed = next(x for x in items if "Men's" in x["marketingName"])
    reg = mixed["priceRange"]["regularPrice"]
    lo = round(reg * 0.8, 2)
    for k in site.plp:                                   # 基线时：一个颜色 8 折，其他颜色原价
        if k.startswith(M + "|"):
            for x in site.plp[k]:
                if x["id"] == mixed["id"]:
                    x["priceRange"].update(minDiscountPrice=lo, maxDiscountPrice=reg)
    cfg, sdir, site, t = _baseline(site=site)
    for k in site.plp:
        if k.startswith(M + "|"):
            for x in site.plp[k]:
                if x["id"] == mixed["id"]:
                    x["priceRange"]["maxDiscountPrice"] = lo          # 原价的颜色也降到折扣价
    plain = next(x for x in items if x["id"] != mixed["id"] and "Men's" in x["marketingName"]
                 and x["priceRange"]["minDiscountPrice"] == x["priceRange"]["regularPrice"])
    plain["colourOptions"].append({"label": "Brand New Teal", "badges": [], "selected": False})
    sent = _pushes_after(cfg, sdir, site, t)
    body = "\n".join(p.body for w, p in sent if w == "我")
    print("\n" + body)
    assert f"{mixed['marketingName']} 更多颜色打折" in body, body
    assert f"{plain['marketingName']} 新配色：Brand New Teal，现价" in body, body
    assert _pushes_after(cfg, sdir, site, t + timedelta(minutes=10)) == []      # 不重复推


def test_config_checks():
    from arcmon.config import ConfigError, load_config
    d = tempfile.mkdtemp()
    path = os.path.join(d, "c.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write("sites: [outlet]\nsubscribers:\n  - name: a\n    bark: bark_me\n    watch:\n"
                "      - {url: outlet.arcteryx.com/de/en/shop/mens/beta-jacket-0511, sizes: [M]}\n")
    os.environ["SECRETS_JSON"] = json.dumps({"BARK_ME": "https://api.day.app/KEY123/hi"})
    try:
        c = load_config(path)
        r = c.subscribers[0].watch[0]
        assert r.sites == ["outlet"] and r.slug == "mens/beta-jacket-0511"
        assert c.subscribers[0].bark.key == "KEY123"
        with open(path, "w", encoding="utf-8") as f:
            f.write("sites: [outlet]\nsubscribers:\n  - name: a\n    watch:\n      - {site: main, keywords: [x]}\n")
        try:
            load_config(path)
            raise AssertionError("应当报错")
        except ConfigError:
            pass
    finally:
        os.environ.pop("SECRETS_JSON", None)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"✓ {name}")
    print("\n全部通过")
