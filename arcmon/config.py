"""读取 config.yaml，解析订阅人、全站动态偏好和关注规则。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import yaml

log = logging.getLogger(__name__)

SITES = {
    "main": {"host": "arcteryx.com", "label": "正价站"},
    "outlet": {"host": "outlet.arcteryx.com", "label": "奥莱"},
}
DEFAULT_CATEGORIES = ["mens", "womens", "accessories", "packs", "footwear"]
FEED_KINDS = {
    "new_product": "new_products",
    "new_colour": "new_colours",
    "price_drop": "price_drops",
    "colour_sale": "price_drops",
    "back_in_catalog": "back_in_catalog",
    "promo_start": "promo",
    "promo_end": "promo",
    "banner_change": "banner",
}
SITE_LEVEL = {"promo_start", "promo_end", "banner_change"}   # 整站级别的事件，不按商品过滤


class ConfigError(Exception):
    pass


def _list(v) -> list:
    if v is None:
        return []
    if isinstance(v, (str, int, float)):
        return [v]
    return list(v)


def _sites(v, default: list[str]) -> list[str]:
    if v in (None, "all"):
        return list(default)
    out = [str(s).strip().lower() for s in _list(v)]
    for s in out:
        if s not in SITES:
            raise ConfigError(f"未知站点 {s!r}，只能是 main / outlet")
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def size_matches(label: str, wants: list[str]) -> bool:
    """官网尺码 label 是否属于想要的尺码。裤子 "32-R" 能被 "32" 匹配。"""
    s = _norm(label)
    return any(s == _norm(w) or s.split("-")[0] == _norm(w) for w in wants)


def discount_pct(price, regular) -> int:
    if not price or not regular or price >= regular:
        return 0
    return round((1 - price / regular) * 100)


@dataclass
class BarkTarget:
    server: str
    key: str


def parse_bark(value: str | None) -> BarkTarget | None:
    """接受纯 key，或从 Bark App 里复制的完整链接 https://api.day.app/KEY/..."""
    if not value:
        return None
    value = value.strip()
    if value.startswith("http"):
        u = urlparse(value)
        parts = [p for p in u.path.split("/") if p]
        if not parts:
            return None
        return BarkTarget(server=f"{u.scheme}://{u.netloc}", key=parts[0])
    return BarkTarget(server="https://api.day.app", key=value)


def _secret(name: str) -> str | None:
    """从环境变量读取（GitHub Actions 在 workflow 的 env 里把 Secret 传进来）。"""
    if not name:
        return None
    for n in (name, name.upper()):          # GitHub 的 Secret 名字一律是大写
        if os.environ.get(n):
            return os.environ[n]
    raw = os.environ.get("SECRETS_JSON")
    if raw:
        try:
            secrets = {k.upper(): v for k, v in json.loads(raw).items()}
        except (json.JSONDecodeError, AttributeError):
            secrets = {}
        if secrets.get(name.upper()):
            return secrets[name.upper()]
    return None


@dataclass
class WatchRule:
    name: str
    sites: list[str]
    slug: str | None = None          # 指定具体商品（来自 url）
    keywords: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    gender: str | None = None
    sizes: list[str] = field(default_factory=list)
    colours: list[str] = field(default_factory=list)
    max_price: float | None = None
    min_discount: int | None = None
    rid: str = ""

    def matches_listing(self, site: str, p: dict) -> bool:
        """用商品列表数据做粗筛（尺码/颜色要等抓详情页后再判断）。"""
        if site not in self.sites:
            return False
        if self.slug:
            return p.get("slug") == self.slug
        name = _norm(p.get("name") or "")
        if any(_norm(k) not in name for k in self.keywords):
            return False
        if any(_norm(k) in name for k in self.exclude):
            return False
        if self.gender and p.get("gender") not in (self.gender, "unisex"):
            return False
        if self.max_price is not None and (p.get("min") or 0) > self.max_price:
            return False
        if self.min_discount and discount_pct(p.get("min"), p.get("price")) < self.min_discount:
            # 列表里只有最低价，用它判断折扣上限
            return False
        return True

    def wants_variant(self, v: dict, has_sizes: bool) -> bool:
        if self.sizes and has_sizes and not size_matches(v["size"], self.sizes):
            return False
        if self.colours:
            # 整名匹配；双色款（"Black / Mongoose"）写其中一个也算。"Black" 不会匹配 "Black Sapphire"
            label = _norm(v["colour"])
            parts = [p.strip() for p in label.split("/")]
            if not any(_norm(c) == label or _norm(c) in parts for c in self.colours):
                return False
        if self.max_price is not None and (v.get("price") is None or v["price"] > self.max_price):
            return False
        if self.min_discount and discount_pct(v.get("price"), v.get("regular")) < self.min_discount:
            return False
        return True


@dataclass
class Feed:
    new_products: list[str] = field(default_factory=list)
    new_colours: list[str] = field(default_factory=list)
    price_drops: list[str] = field(default_factory=list)
    back_in_catalog: list[str] = field(default_factory=list)
    promo: list[str] = field(default_factory=list)     # 全场折上折 / 打折开始、结束
    banner: list[str] = field(default_factory=list)    # 首页活动横幅变化
    gender: str | None = None
    max_price: float | None = None
    min_discount: int | None = None
    sizes: list[str] = field(default_factory=list)     # 推送里标出这些尺码哪些颜色有货

    def wants(self, ev: dict) -> bool:
        kind = FEED_KINDS.get(ev["type"])
        if not kind or ev["site"] not in getattr(self, kind):
            return False
        if ev.get("brief") or ev.get("quiet"):
            return False    # 短暂下架又回来 / 同一场活动重复识别：只记录不推送
        return ev["type"] in SITE_LEVEL or self.passes_filters(ev)

    def passes_filters(self, ev: dict) -> bool:
        if self.gender and ev.get("gender") not in (self.gender, "unisex"):
            return False
        if self.max_price is not None and (ev.get("min") or 0) > self.max_price:
            return False
        if self.min_discount and discount_pct(ev.get("min"), ev.get("price")) < self.min_discount:
            return False
        return True


@dataclass
class Subscriber:
    name: str
    bark: BarkTarget | None
    admin: bool
    digest: bool
    feed: Feed
    watch: list[WatchRule]


@dataclass
class Config:
    country: str
    language: str
    sites: list[str]
    categories: list[str]
    max_detail_pages: int
    subscribers: list[Subscriber]

    @property
    def admins(self) -> list[Subscriber]:
        a = [s for s in self.subscribers if s.admin]
        return a or self.subscribers[:1]


def _slug_from_url(url: str) -> tuple[str, str]:
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    u = urlparse(url)
    site = "outlet" if u.netloc.startswith("outlet.") else "main"
    if "/shop/" not in u.path:
        raise ConfigError(f"商品链接里找不到 /shop/：{url}")
    slug = u.path.split("/shop/", 1)[1].strip("/")
    return site, slug


def _parse_rule(raw: dict, sites: list[str], owner: str, idx: int) -> WatchRule:
    if not isinstance(raw, dict):
        raise ConfigError(f"{owner} 的第 {idx + 1} 条关注规则格式不对")
    gender = raw.get("gender")
    if gender and gender not in ("mens", "womens"):
        raise ConfigError(f"gender 只能是 mens / womens：{gender}")
    rule = WatchRule(
        name=str(raw.get("name") or f"规则{idx + 1}"),
        sites=_sites(raw.get("site", raw.get("sites")), sites),
        keywords=[str(k) for k in _list(raw.get("keywords"))],
        exclude=[str(k) for k in _list(raw.get("exclude"))],
        gender=gender,
        sizes=[str(s) for s in _list(raw.get("sizes"))],
        colours=[str(c) for c in _list(raw.get("colours", raw.get("colors")))],
        max_price=float(raw["max_price"]) if raw.get("max_price") is not None else None,
        min_discount=int(raw["min_discount"]) if raw.get("min_discount") else None,
    )
    if raw.get("url"):
        site, rule.slug = _slug_from_url(raw["url"])
        rule.sites = [site]
    off = [s for s in rule.sites if s not in sites]
    if off:
        raise ConfigError(f"{owner} 的规则「{rule.name}」用到了 {off}，但顶部 sites 里没开这个站")
    if not rule.slug and not rule.keywords and not rule.gender:
        raise ConfigError(f"{owner} 的规则「{rule.name}」需要 url、keywords 或 gender 至少一个，否则会匹配全站")
    # 规则内容变了就当作新规则，重新建立初始状态
    sig = json.dumps(
        [rule.sites, rule.slug, rule.keywords, rule.exclude, rule.gender, rule.sizes,
         rule.colours, rule.max_price, rule.min_discount], ensure_ascii=False)
    rule.rid = hashlib.sha1(sig.encode()).hexdigest()[:10]
    return rule


def load_config(path: str = "config.yaml") -> Config:
    text = os.environ.get("MONITOR_CONFIG") or _secret("MONITOR_CONFIG")
    if not text:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    raw = yaml.safe_load(text) or {}

    sites = _sites(raw.get("sites"), list(SITES))
    subs = []
    names = set()
    for i, s in enumerate(_list(raw.get("subscribers"))):
        name = str(s.get("name") or f"订阅人{i + 1}")
        if name in names:
            raise ConfigError(f"订阅人名字重复：{name}")
        names.add(name)
        bark_ref = s.get("bark")
        bark_val = _secret(str(bark_ref)) if bark_ref else None
        if not bark_val and bark_ref and str(bark_ref).startswith("http"):
            bark_val = str(bark_ref)
        if bark_ref and not bark_val:
            log.warning("找不到 %s 的 Bark key（%s），将只打印不推送", name, bark_ref)
        f = s.get("feed") or {}
        feed = Feed(
            new_products=_sites(f["new_products"], sites) if f.get("new_products") else [],
            new_colours=_sites(f["new_colours"], sites) if f.get("new_colours") else [],
            price_drops=_sites(f["price_drops"], sites) if f.get("price_drops") else [],
            back_in_catalog=_sites(f["back_in_catalog"], sites) if f.get("back_in_catalog") else [],
            promo=_sites(f["promo"], sites) if f.get("promo") else [],
            banner=_sites(f["banner"], sites) if f.get("banner") else [],
            sizes=[str(x) for x in _list(f.get("sizes"))],
            gender=f.get("gender"),
            max_price=float(f["max_price"]) if f.get("max_price") is not None else None,
            min_discount=int(f["min_discount"]) if f.get("min_discount") else None,
        )
        rules = [_parse_rule(r, sites, name, j) for j, r in enumerate(_list(s.get("watch")))]
        subs.append(Subscriber(
            name=name,
            bark=parse_bark(bark_val),
            admin=bool(s.get("admin")),
            digest=bool(s.get("daily_digest", True)),
            feed=feed,
            watch=rules,
        ))
    if not subs:
        raise ConfigError("config.yaml 里至少要有一个 subscribers")

    return Config(
        country=str(raw.get("country", "de")).lower(),
        language=str(raw.get("language", "en")).lower(),
        sites=sites,
        categories=[str(c).strip("/") for c in _list(raw.get("categories"))] or DEFAULT_CATEGORIES,
        max_detail_pages=int(raw.get("max_detail_pages", 40)),
        subscribers=subs,
    )
