"""抓取始祖鸟德国正价站 / 奥莱的数据。

两个站点是同一套系统：
- 商品列表：/api/catalog.getProductListingPage（tRPC 接口，每页最多 100 条，带价格和配色）
- 商品详情：/catalog-pages/_next/data/<buildId>/<国家>/<语言>/shop/<slug>.json
  里面的 variants 有每个 颜色×尺码 的 stockStatus（InStock / LowStock / OutOfStock）
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from urllib.parse import quote

from .config import SITES

log = logging.getLogger(__name__)

AVAILABLE = {"InStock", "LowStock"}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")


class FetchError(Exception):
    pass


class BlockedError(FetchError):
    """被拦截（403/返回网页而不是数据）。"""


class NotFound(FetchError):
    pass


class Http:
    """优先用 curl_cffi 模拟 Chrome 的 TLS 指纹，装不上就退回 requests。请求之间随机停顿。"""

    def __init__(self, min_delay: float = 0.8, max_delay: float = 1.8):
        self.min_delay, self.max_delay = min_delay, max_delay
        self._last = 0.0
        try:
            from curl_cffi import requests as creq
            self._s = creq.Session(impersonate="chrome")
            self.engine = "curl_cffi"
        except Exception:  # noqa: BLE001
            import requests
            self._s = requests.Session()
            self._s.headers["User-Agent"] = UA
            self.engine = "requests"

    def _pause(self):
        wait = self._last + random.uniform(self.min_delay, self.max_delay) - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str, *, html: bool = False, referer: str | None = None) -> tuple[int, str]:
        headers = {
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                       if html else "application/json, text/plain, */*"),
            "Accept-Language": "en-GB,en;q=0.9,de;q=0.8",
        }
        if referer:
            headers["Referer"] = referer
        err: Exception | None = None
        for attempt in range(3):
            self._pause()
            try:
                r = self._s.get(url, headers=headers, timeout=20)
            except Exception as e:  # noqa: BLE001
                err = e
                time.sleep(3 * (attempt + 1))
                continue
            finally:
                self._last = time.monotonic()
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                err = FetchError(f"HTTP {r.status_code}")
                time.sleep(5 * (attempt + 1))
                continue
            return r.status_code, r.text
        raise FetchError(f"请求失败：{err}")


def _parse_json(status: int, text: str, url: str):
    if status == 404:
        raise NotFound(url)
    if status in (401, 403, 429):
        raise BlockedError(f"HTTP {status}（可能被反爬拦截）")
    if status != 200:
        raise FetchError(f"HTTP {status}: {text[:200]}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if "<html" in text[:500].lower():
            raise BlockedError("返回的是网页而不是数据（可能被反爬拦截）") from None
        raise FetchError(f"无法解析返回内容：{text[:200]}") from None


def _gender(slug: str, name: str) -> str:
    if slug.startswith("mens/") or "Men's" in name:
        return "mens"
    if slug.startswith("womens/") or "Women's" in name:
        return "womens"
    return "unisex"


def norm_listing(p: dict) -> dict:
    pr = p.get("priceRange") or {}
    regular = pr.get("regularPrice")
    lo = pr.get("minDiscountPrice")
    lo = regular if lo is None else lo
    hi = pr.get("maxDiscountPrice")
    hi = lo if hi is None else hi
    opts = p.get("colourOptions") or []
    name = p.get("marketingName") or p.get("analyticsName") or p.get("id")
    img = None
    for c in opts:
        img = (c.get("thumbnail") or {}).get("url")
        if img:
            break
    img = img or (p.get("hoverImage") or {}).get("url")
    return {
        "id": p["id"],
        "name": name,
        "slug": p["slug"],
        "gender": _gender(p["slug"], name),
        "price": regular,
        "min": lo,
        "max": hi,
        "colours": sorted({c["label"] for c in opts if c.get("label")}),
        "is_new": bool(p.get("isNew")),
        "img": img,
    }


def norm_detail(page: dict) -> dict | None:
    props = page.get("pageProps") or {}
    if "__N_REDIRECT" in props:
        return None
    p = props.get("product")
    if isinstance(p, str):
        p = json.loads(p)
    if not p:
        return None
    colours = {str(c.get("id")): c.get("label") for c in p.get("colourOptions") or []}
    sizes = {str(o.get("value")): o.get("label") for o in (p.get("sizeOptions") or {}).get("options") or []}
    variants = []
    for v in p.get("variants") or []:
        cid, sid = str(v.get("colourId")), str(v.get("sizeId"))
        variants.append({
            "colour": colours.get(cid) or cid,
            "size": sizes.get(sid) or ("均码" if not sizes else sid),
            "status": v.get("stockStatus") or "Unknown",
            "price": v.get("discountPrice") if v.get("discountPrice") is not None else v.get("price"),
            "regular": v.get("price"),
        })
    return {
        "id": p.get("id"),
        "name": p.get("marketingName") or p.get("name"),
        "slug": p.get("slug"),
        "img": (p.get("mainImage") or {}).get("url"),
        "has_sizes": bool(sizes),
        "variants": variants,
    }


class SiteClient:
    def __init__(self, http, site: str, country: str = "de", language: str = "en",
                 build_id: str | None = None):
        self.http = http
        self.site = site
        self.host = SITES[site]["host"]
        self.label = SITES[site]["label"]
        self.country, self.language = country, language
        self.build_id = build_id
        self._build_refreshed = False

    @property
    def base(self) -> str:
        return f"https://{self.host}/{self.country}/{self.language}"

    def product_url(self, slug: str) -> str:
        return f"{self.base}/shop/{slug}"

    # ---------- 商品列表 ----------
    def listing_page(self, category: str, offset: int = 0, limit: int = 100) -> dict:
        page_url = f"{self.base}/c/{category}"
        inp = {
            "json": {"browserUserId": None, "country": self.country, "filters": {},
                     "language": self.language, "limit": limit, "offset": offset,
                     "slug": f"{category}/", "sort": "", "url": page_url},
            "meta": {"values": {"browserUserId": ["undefined"]}},
        }
        url = (f"https://{self.host}/api/catalog.getProductListingPage?input="
               + quote(json.dumps(inp, separators=(",", ":"))))
        status, text = self.http.get(url, referer=page_url)
        data = _parse_json(status, text, url)
        try:
            return data["result"]["data"]["json"]
        except (KeyError, TypeError):
            raise FetchError(f"列表接口返回格式变了：{str(data)[:200]}") from None

    def listing(self, category: str) -> list[dict]:
        items, offset = [], 0
        while True:
            d = self.listing_page(category, offset)
            items.extend(d.get("productList") or [])
            pag = d.get("pagination") or {}
            if not pag.get("hasMore"):
                total = pag.get("totalCount")
                if total is not None and len(items) < total:
                    raise FetchError(f"{category} 只拿到 {len(items)}/{total} 条，数据不完整")
                return items
            offset += pag.get("limit") or 100
            if offset > 3000:
                raise FetchError("分页异常")

    def catalog(self, categories: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for cat in categories:
            for p in self.listing(cat):
                if p.get("id") and p.get("slug"):
                    out.setdefault(p["id"], norm_listing(p))
        if not out:
            raise FetchError("商品列表为空")
        return out

    # ---------- 商品详情（尺码库存） ----------
    def refresh_build_id(self) -> str:
        url = f"{self.base}/c/{'mens'}"
        status, text = self.http.get(url, html=True)
        if status in (401, 403, 429):
            raise BlockedError(f"HTTP {status}（可能被反爬拦截）")
        m = re.search(r'"buildId"\s*:\s*"([^"]+)"', text)
        if status != 200 or not m:
            raise FetchError(f"拿不到 buildId（HTTP {status}）")
        self.build_id = m.group(1)
        self._build_refreshed = True
        log.info("%s buildId = %s", self.site, self.build_id)
        return self.build_id

    def detail(self, slug: str) -> dict | None:
        """返回标准化的详情；商品不存在返回 None。"""
        if not self.build_id:
            self.refresh_build_id()
        for _ in range(2):
            url = (f"https://{self.host}/catalog-pages/_next/data/{self.build_id}/"
                   f"{self.country}/{self.language}/shop/{slug}.json")
            status, text = self.http.get(url, referer=self.product_url(slug))
            if status == 404 and not self._build_refreshed:
                # 网站重新部署后 buildId 会变，旧的会 404
                self.refresh_build_id()
                continue
            if status == 404:
                return None
            return norm_detail(_parse_json(status, text, url))
        return None
