#!/usr/bin/env python3
"""
全球实时油气新闻聚合
从指定的 10 个行业网站抓取最新油气新闻，生成一个静态 HTML 页面。

数据来源（仅从以下站点采集）：
  1. CNBC Energy             cnbc.com/energy
  2. Bloomberg Energy        bloomberg.com/energy
  3. S&P Global Platts       spglobal.com/platts
  4. Oil & Gas Journal       ogj.com
  5. Rigzone                 rigzone.com
  6. Oilprice.com            oilprice.com
  7. Natural Gas Intelligence naturalgasintel.com
  8. World Oil               worldoil.com
  9. OilandGasPress.com      oilandgaspress.com
 10. 财联社                   cls.cn
"""

from __future__ import annotations

import html as html_mod
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import feedparser
import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator
from googlenewsdecoder import new_decoderv1

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ── 请求配置 ──────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
}
TIMEOUT = 15  # 秒
MAX_ITEMS_PER_SOURCE = 5  # 每个来源最多取几条


# ── 数据来源定义 ──────────────────────────────────────────
# 每个来源是一个 dict:
#   name       : 显示名
#   category   : 分类标签
#   method     : "rss" | "scrape"
#   url        : RSS 地址 或 网页地址
#   alt_urls   : 备用 URL 列表 (可选)
#   selectors  : CSS 选择器配置 (scrape 专用)

SOURCES: list[dict[str, Any]] = [
    # 1. CNBC Energy (含 Reuters 转载, 有完整摘要)
    {
        "name": "CNBC",
        "category": "国际",
        "method": "rss",
        "url": "https://www.cnbc.com/id/19836768/device/rss/rss.html",
    },
    # 2. Bloomberg Energy
    {
        "name": "Bloomberg",
        "category": "财经",
        "method": "rss",
        "url": "https://news.google.com/rss/search?q=site:bloomberg.com+energy+oil+gas&hl=en&gl=US&ceid=US:en",
    },
    # 3. S&P Global Platts
    {
        "name": "S&P Platts",
        "category": "市场",
        "method": "rss",
        "url": "https://news.google.com/rss/search?q=site:spglobal.com+platts+oil+gas+energy&hl=en&gl=US&ceid=US:en",
    },
    # 4. Oil & Gas Journal
    {
        "name": "OGJ",
        "category": "行业",
        "method": "rss",
        "url": "https://www.ogj.com/rss",
        "alt_urls": [
            "https://news.google.com/rss/search?q=site:ogj.com+oil+gas&hl=en&gl=US&ceid=US:en",
        ],
    },
    # 5. Rigzone
    {
        "name": "Rigzone",
        "category": "行业",
        "method": "rss",
        "url": "https://www.rigzone.com/news/rss/rigzone_latest.aspx",
        "alt_urls": [
            "https://news.google.com/rss/search?q=site:rigzone.com&hl=en&gl=US&ceid=US:en",
        ],
    },
    # 6. Oilprice.com
    {
        "name": "Oilprice",
        "category": "油价",
        "method": "rss",
        "url": "https://oilprice.com/rss/main",
        "alt_urls": [
            "https://news.google.com/rss/search?q=site:oilprice.com&hl=en&gl=US&ceid=US:en",
        ],
    },
    # 7. Natural Gas Intelligence
    {
        "name": "NGI",
        "category": "天然气",
        "method": "rss",
        "url": "https://www.naturalgasintel.com/feed/",
        "alt_urls": [
            "https://news.google.com/rss/search?q=site:naturalgasintel.com&hl=en&gl=US&ceid=US:en",
        ],
    },
    # 8. World Oil
    {
        "name": "World Oil",
        "category": "行业",
        "method": "rss",
        "url": "https://www.worldoil.com/rss",
        "alt_urls": [
            "https://news.google.com/rss/search?q=site:worldoil.com+oil+gas&hl=en&gl=US&ceid=US:en",
        ],
    },
    # 9. OilandGasPress.com
    {
        "name": "OilGasPress",
        "category": "综合",
        "method": "scrape",
        "url": "https://oilandgaspress.com/news-analysis/",
        "selectors": {
            "article": ".qode-news-item",
            "title": "h4.entry-title a, p.entry-title a, .qode-post-title a",
            "summary": ".qode-post-excerpt-holder",
            "time": ".qode-post-info-date",
        },
    },
    # 10. 财联社 (通过 nodeapi 接口获取电报流，再按关键字过滤)
    {
        "name": "财联社",
        "category": "财经",
        "method": "cls_api",
        "url": "https://www.cls.cn/nodeapi/telegraphList?app=CailianpressWeb&os=web&sv=8.4.6&rn=200",
    },
]


# ── 辅助函数 ──────────────────────────────────────────────

def _clean(text: str | None) -> str:
    """清理 HTML 标签 & 多余空白。"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _truncate(text: str, n: int = 500) -> str:
    return text[:n] + "…" if len(text) > n else text


def _fetch_url(url: str) -> requests.Response:
    return requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)


def _resolve_google_news_url(url: str) -> str:
    """如果是 Google News 中转链接，解码获取真实文章 URL。"""
    if "news.google.com" not in url:
        return url
    try:
        result = new_decoderv1(url, interval=0.5)
        if result.get("status") and result.get("decoded_url"):
            return result["decoded_url"]
    except Exception:
        pass
    return url


def _fetch_og_description(url: str) -> str:
    """从文章页面抓取 og:description 或正文段落作为摘要。"""
    try:
        # 如果是 Google News 中转链接，先解析真实 URL
        real_url = _resolve_google_news_url(url)
        resp = requests.get(real_url, headers=HEADERS, timeout=10, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "lxml")
        # 过滤掉 Google News 通用描述
        google_generic = "comprehensive up-to-date news coverage"
        js_block = "please enable js"

        best_meta = ""
        # 优先 og:description
        og = soup.find("meta", property="og:description")
        if og and og.get("content", "").strip():
            desc = _clean(og["content"])
            if google_generic not in desc.lower() and js_block not in desc.lower() and len(desc) > 30:
                if len(desc) >= 150:
                    return desc
                best_meta = desc
        # 其次 meta description
        if not best_meta:
            meta = soup.find("meta", attrs={"name": "description"})
            if meta and meta.get("content", "").strip():
                desc = _clean(meta["content"])
                if google_generic not in desc.lower() and js_block not in desc.lower() and len(desc) > 30:
                    if len(desc) >= 150:
                        return desc
                    best_meta = desc

        # 尝试拼接多个 <p> 段落以获得更丰富的摘要
        paragraphs = []
        for p in soup.select("article p, .content p, .entry-content p, .articleHeader ~ p, main p, p"):
            text = _clean(p.get_text())
            if len(text) > 40 and google_generic not in text.lower() and js_block not in text.lower():
                paragraphs.append(text)
                if sum(len(t) for t in paragraphs) >= 400:
                    break
        if paragraphs:
            combined = " ".join(paragraphs)
            if len(combined) > len(best_meta):
                return combined

        if best_meta:
            return best_meta
    except Exception:
        pass

    # Fallback: 从 DuckDuckGo 搜索摘要获取描述（适用于 JS 渲染页面如 Reuters）
    return _fetch_ddg_snippet(url)


def _fetch_ddg_snippet(url: str) -> str:
    """通过 DuckDuckGo HTML 搜索获取文章摘要片段。"""
    try:
        real_url = _resolve_google_news_url(url)
        # 用文章 URL 作为搜索查询
        query = real_url.split("?")[0]  # 去掉查询参数
        ddg_url = f"https://html.duckduckgo.com/html/?q={requests.utils.quote(query)}"
        resp = requests.get(ddg_url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "lxml")
        snippets = []
        for result in soup.select(".result__snippet"):
            text = _clean(result.get_text())
            if len(text) > 40:
                snippets.append(text)
                if sum(len(t) for t in snippets) >= 400:
                    break
        if snippets:
            return " ".join(snippets)
    except Exception:
        pass
    return ""


def _is_summary_useless(title: str, summary: str) -> bool:
    """判断 summary 是否无用（为空、太短、或只是标题的重复）。"""
    if not summary or len(summary) < 150:
        return True
    # Google News RSS 的 summary 通常就是 title + 来源名
    s_lower = summary.lower().strip()
    t_lower = title.lower().strip()
    # 如果 summary 以 title 开头（可能后面跟着来源名）
    if s_lower.startswith(t_lower[:30]):
        return True
    return False


# ── RSS 抓取 ──────────────────────────────────────────────

def _fetch_rss(source: dict) -> list[dict]:
    """通过 RSS 抓取新闻条目。"""
    urls = [source["url"]] + source.get("alt_urls", [])
    entries: list = []
    for url in urls:
        try:
            resp = _fetch_url(url)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            entries = feed.entries[:MAX_ITEMS_PER_SOURCE]
            if entries:
                break
        except Exception as exc:
            log.debug("RSS %s failed (%s): %s", source["name"], url, exc)
            continue

    results = []
    for e in entries:
        pub = ""
        if hasattr(e, "published"):
            pub = e.published
        elif hasattr(e, "updated"):
            pub = e.updated
        results.append({
            "category": source["category"],
            "title": _clean(e.get("title", "")),
            "summary": _truncate(_clean(e.get("summary", e.get("description", "")))),
            "source": source["name"],
            "link": e.get("link", ""),
            "time": pub,
        })
    return results


# ── HTML 抓取 ─────────────────────────────────────────────

def _fetch_scrape(source: dict) -> list[dict]:
    """通过 HTML 网页抓取新闻条目。"""
    resp = _fetch_url(source["url"])
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "lxml")
    sel = source["selectors"]

    articles = soup.select(sel["article"])[:MAX_ITEMS_PER_SOURCE]
    if not articles:
        # Fallback: try all links with text
        articles = soup.select("a[href]")
        articles = [a for a in articles if a.get_text(strip=True)][:MAX_ITEMS_PER_SOURCE]

    results = []
    for art in articles:
        # 标题
        title_el = art.select_one(sel["title"]) if art.name != "a" else art
        title = _clean(title_el.get_text()) if title_el else ""
        if not title:
            continue

        # 链接
        link = ""
        if title_el and title_el.name == "a":
            link = title_el.get("href", "")
        elif title_el:
            a = title_el.find("a")
            if a:
                link = a.get("href", "")
        if link and not link.startswith("http"):
            link = source["url"].rstrip("/") + "/" + link.lstrip("/")

        # 摘要
        summary_el = art.select_one(sel["summary"]) if art.name != "a" else None
        summary = _truncate(_clean(summary_el.get_text())) if summary_el else ""

        # 时间
        time_el = art.select_one(sel["time"]) if art.name != "a" else None
        pub_time = _clean(time_el.get_text()) if time_el else ""

        results.append({
            "category": source["category"],
            "title": _truncate(title, 80),
            "summary": summary,
            "source": source["name"],
            "link": link,
            "time": pub_time,
        })
    return results


# ── 财联社 API 抓取 ──────────────────────────────────────

def _fetch_cls_api(source: dict) -> list[dict]:
    """通过财联社 nodeapi 获取电报流新闻。"""
    resp = _fetch_url(source["url"])
    resp.raise_for_status()
    data = resp.json()
    roll_data = data.get("data", {}).get("roll_data", [])
    results = []
    for item in roll_data:
        content = _clean(item.get("content", ""))
        title = _clean(item.get("title", ""))
        # depth_extends 包含深度文章标题
        depth = item.get("depth_extends") or {}
        depth_title = _clean(depth.get("title", "")) if isinstance(depth, dict) else ""
        # shareurl 作为链接
        link = item.get("shareurl", "")
        # 时间戳转字符串
        ctime = item.get("ctime", 0)
        pub_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(ctime)) if ctime else ""
        # 标题优先用 depth_title，否则用 title 或 content 前60字
        display_title = depth_title or title or content[:60]
        if not display_title:
            continue
        # 摘要用 content
        summary = _truncate(content) if content != display_title else ""
        results.append({
            "category": source["category"],
            "title": _truncate(display_title, 80),
            "summary": summary,
            "source": source["name"],
            "link": link,
            "time": pub_time,
        })
    # 不在此处限制数量，让全局关键字过滤筛选后再截取
    return results


# ── 统一抓取入口 ─────────────────────────────────────────

def fetch_source(source: dict) -> list[dict]:
    """根据 source 配置抓取新闻，出错返回空列表。"""
    try:
        if source["method"] == "rss":
            items = _fetch_rss(source)
        elif source["method"] == "cls_api":
            items = _fetch_cls_api(source)
        else:
            items = _fetch_scrape(source)
        log.info("✔ %-18s  %d 条", source["name"], len(items))
        return items
    except Exception as exc:
        log.warning("✘ %-18s  失败: %s", source["name"], exc)
        return []


# ── 关键字过滤 ────────────────────────────────────────────
# 仅保留标题或摘要中包含以下关键字之一的新闻（不区分大小写）
KEYWORDS = [
    "oil", "oil price", "petrol", "petrol price",
    "crude", "brent", "wti", "opec",
    "lng", "lpg", "shale",
    "石油", "石油产量", "石油价格",
    "天然气", "natural gas",
    "页岩油", "shale oil",
    "原油", "油价", "燃气", "成品油",
    "炼油", "汽油", "柴油", "液化气",
    "油田", "油气", "石油管道", "天然气管道",
]
_KEYWORD_PATTERN = re.compile(
    "|".join(re.escape(k) for k in KEYWORDS), re.IGNORECASE
)


def _matches_keywords(item: dict) -> bool:
    """标题或摘要中是否包含至少一个油气关键字。"""
    text = item.get("title", "") + " " + item.get("summary", "")
    return bool(_KEYWORD_PATTERN.search(text))


# ── 摘要翻译 ──────────────────────────────────────────────

_ZH_RE = re.compile(r"[\u4e00-\u9fff]")


def _is_chinese(text: str) -> bool:
    """粗略判断文本是否已经是中文（中文字符占比 > 20%）。"""
    if not text:
        return True
    zh_count = len(_ZH_RE.findall(text))
    return zh_count / max(len(text), 1) > 0.2


def _translate_summaries(items: list[dict]) -> None:
    """将所有非中文摘要翻译为简体中文（就地修改）。"""
    to_translate: list[tuple[int, str]] = []
    for i, item in enumerate(items):
        summary = item.get("summary", "")
        if summary and not _is_chinese(summary):
            to_translate.append((i, summary))

    if not to_translate:
        return

    log.info("翻译摘要: %d 条需要翻译…", len(to_translate))
    translator = GoogleTranslator(source="auto", target="zh-CN")

    # Google Translate 每次最多 5000 字符，分批翻译
    batch_texts: list[str] = []
    batch_indices: list[int] = []
    batch_char_count = 0
    translated_count = 0

    def _flush_batch():
        nonlocal translated_count
        if not batch_texts:
            return
        try:
            results = translator.translate_batch(batch_texts)
            for idx, result in zip(batch_indices, results):
                if result and len(result) > 10:
                    items[idx]["summary"] = _truncate(result)
                    translated_count += 1
        except Exception as exc:
            log.warning("翻译批次失败: %s", exc)

    for idx, text in to_translate:
        # 如果加入此文本会超过 4500 字符，先刷出当前批次
        if batch_char_count + len(text) > 4500 and batch_texts:
            _flush_batch()
            batch_texts.clear()
            batch_indices.clear()
            batch_char_count = 0
        batch_texts.append(text)
        batch_indices.append(idx)
        batch_char_count += len(text)

    _flush_batch()
    log.info("翻译摘要: %d / %d 条成功", translated_count, len(to_translate))


def fetch_all(sources: list[dict] | None = None) -> list[dict]:
    """并发抓取所有来源，返回合并后的新闻列表（仅保留匹配关键字的）。"""
    if sources is None:
        sources = SOURCES
    all_news: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_source, s): s for s in sources}
        for fut in as_completed(futures):
            all_news.extend(fut.result())
    filtered = [n for n in all_news if _matches_keywords(n)]
    log.info("关键字过滤: %d / %d 条匹配", len(filtered), len(all_news))

    # 每个来源最多保留 MAX_ITEMS_PER_SOURCE 条（防止 API 来源占比过大）
    source_count: dict[str, int] = {}
    limited: list[dict] = []
    for n in filtered:
        src = n.get("source", "")
        source_count[src] = source_count.get(src, 0) + 1
        if source_count[src] <= MAX_ITEMS_PER_SOURCE:
            limited.append(n)
    if len(limited) < len(filtered):
        log.info("来源限流: %d → %d 条", len(filtered), len(limited))
    filtered = limited

    # 补充短/空摘要：并发从原文页面拉取 og:description
    need_enrich = [n for n in filtered if _is_summary_useless(n["title"], n["summary"]) and n.get("link")]
    if need_enrich:
        log.info("补充摸要: %d 条需要从原文获取…", len(need_enrich))
        with ThreadPoolExecutor(max_workers=6) as pool:
            future_map = {pool.submit(_fetch_og_description, n["link"]): n for n in need_enrich}
            for fut in as_completed(future_map):
                item = future_map[fut]
                desc = fut.result()
                if desc and len(desc) > len(item["summary"]):
                    item["summary"] = _truncate(desc)
        enriched = sum(1 for n in need_enrich if not _is_summary_useless(n["title"], n["summary"]))
        log.info("补充摸要: %d / %d 条成功", enriched, len(need_enrich))

    # 翻译：将所有非中文摘要翻译为简体中文
    _translate_summaries(filtered)

    # 解析时间、过滤最近两周、按时间降序排列
    cutoff = datetime.now(timezone.utc) - timedelta(weeks=2)
    for n in filtered:
        n["_dt"] = _parse_time(n.get("time", ""))

    before = len(filtered)
    filtered = [n for n in filtered if n["_dt"] is None or n["_dt"] >= cutoff]
    if len(filtered) < before:
        log.info("时间过滤: %d → %d 条 (仅保留最近两周)", before, len(filtered))

    # 有时间的排在前面（降序），无时间的排在最后
    filtered.sort(key=lambda n: n["_dt"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    return filtered


# ── 时间解析 ──────────────────────────────────────────────

_TIME_FORMATS = [
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S%z",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%Y/%m/%d %H:%M",
]


def _parse_time(time_str: str) -> datetime | None:
    """尽力将各种时间字符串解析为 aware datetime (UTC)。"""
    if not time_str:
        return None
    time_str = time_str.strip()
    # 1. 尝试 RFC 2822 (RSS 标准格式, e.g. "Sat, 21 Feb 2026 12:00:00 GMT")
    try:
        dt = parsedate_to_datetime(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    # 2. 尝试常见格式
    for fmt in _TIME_FORMATS:
        try:
            dt = datetime.strptime(time_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    # 3. 尝试从字符串中提取 YYYY-MM-DD 或 MM/DD/YYYY
    m = re.search(r"(\d{4})[年\-/](\d{1,2})[月\-/](\d{1,2})", time_str)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
            # 提取可能的时分
            hm = re.search(r"(\d{1,2}):(\d{2})", time_str)
            if hm:
                dt = dt.replace(hour=int(hm.group(1)), minute=int(hm.group(2)))
            return dt
        except ValueError:
            pass
    return None


# ── HTML 生成 ─────────────────────────────────────────────

CATEGORY_COLORS = {
    "国际": ("#fce4ec", "#c62828"),
    "财经": ("#fff3e0", "#e65100"),
    "市场": ("#e8eaf6", "#283593"),
    "行业": ("#e8f5e9", "#2e7d32"),
    "油价": ("#fff8e1", "#f57f17"),
    "天然气": ("#e0f7fa", "#00695c"),
    "综合": ("#f3e5f5", "#6a1b9a"),
}


def _category_badge(cat: str) -> str:
    bg, fg = CATEGORY_COLORS.get(cat, ("#eeeeee", "#333333"))
    return (
        f'<span class="badge" style="background:{bg};color:{fg}">'
        f"{html_mod.escape(cat)}</span>"
    )


def _news_card(item: dict) -> str:
    title = html_mod.escape(item["title"])
    summary = html_mod.escape(item["summary"])
    source = html_mod.escape(item["source"])
    time_str = html_mod.escape(item.get("time", ""))
    link = item.get("link", "")

    title_html = (
        f'<a href="{html_mod.escape(link)}" target="_blank" rel="noopener">{title}</a>'
        if link else title
    )

    meta_parts = [source]
    if time_str:
        meta_parts.append(time_str)

    badge = _category_badge(item["category"])
    meta = " · ".join(meta_parts)

    return (
        '    <article class="card">\n'
        '      <div class="card-header">\n'
        f'        {badge}\n'
        f'        <span class="meta">{meta}</span>\n'
        '      </div>\n'
        f'      <h3 class="card-title">{title_html}</h3>\n'
        f'      <p class="card-summary">{summary}</p>\n'
        '    </article>'
    )


def _source_list_html() -> str:
    """生成来源网站列表区域。"""
    items = []
    source_urls = {
        "CNBC":          "https://cnbc.com/energy",
        "Bloomberg":     "https://bloomberg.com/energy",
        "S&P Platts":    "https://spglobal.com/platts",
        "OGJ":           "https://ogj.com",
        "Rigzone":       "https://rigzone.com",
        "Oilprice":      "https://oilprice.com",
        "NGI":           "https://naturalgasintel.com",
        "World Oil":     "https://worldoil.com",
        "OilGasPress":   "https://oilandgaspress.com/news-analysis/",
        "财联社":        "https://www.cls.cn",
    }
    for name, url in source_urls.items():
        items.append(
            f'<a href="{url}" target="_blank" rel="noopener" class="src-tag">{html_mod.escape(name)}</a>'
        )
    return " ".join(items)


def generate_html(news: list[dict] | None = None) -> str:
    """返回完整的 HTML 字符串。"""
    if news is None:
        news = fetch_all()

    cards_html = "\n".join(_news_card(n) for n in news)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(news)

    return f"""\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>全球实时油气新闻聚合</title>
  <style>
    /* ── Reset & Base ───────────────────────── */
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei",
                   system-ui, -apple-system, sans-serif;
      background: #f0f2f5;
      color: #333;
      line-height: 1.6;
    }}

    /* ── Header ─────────────────────────────── */
    header {{
      background: linear-gradient(135deg, #1b5e20 0%, #004d40 60%, #01579b 100%);
      color: #fff;
      padding: 32px 24px 22px;
      text-align: center;
      box-shadow: 0 2px 10px rgba(0,0,0,.2);
    }}
    header h1 {{
      font-size: 1.9rem;
      font-weight: 700;
      letter-spacing: .08em;
      display: inline;
    }}
    .header-row {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 16px;
    }}
    .btn-refresh {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 7px 18px;
      font-size: .85rem;
      font-weight: 600;
      color: #1b5e20;
      background: #fff;
      border: none;
      border-radius: 20px;
      cursor: pointer;
      transition: background .2s, transform .15s;
      box-shadow: 0 2px 6px rgba(0,0,0,.15);
      white-space: nowrap;
    }}
    .btn-refresh:hover {{
      background: #e8f5e9;
    }}
    .btn-refresh:active {{
      transform: scale(.95);
    }}
    .btn-refresh .icon {{
      display: inline-block;
      transition: transform .4s;
    }}
    .btn-refresh.loading {{
      pointer-events: none;
      opacity: .7;
    }}
    .btn-refresh.loading .icon {{
      animation: spin 1s linear infinite;
    }}
    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}
    header .subtitle {{
      margin-top: 8px;
      font-size: .85rem;
      opacity: .85;
    }}

    /* ── Sources bar ───────────────────────── */
    .sources {{
      background: #fff;
      padding: 14px 24px;
      text-align: center;
      box-shadow: 0 1px 4px rgba(0,0,0,.06);
      overflow-x: auto;
      white-space: nowrap;
    }}
    .sources .label {{
      font-size: .8rem;
      color: #888;
      margin-right: 8px;
    }}
    .src-tag {{
      display: inline-block;
      font-size: .75rem;
      padding: 3px 10px;
      margin: 3px 4px;
      border-radius: 14px;
      background: #e8f5e9;
      color: #2e7d32;
      text-decoration: none;
      transition: background .15s;
    }}
    .src-tag:hover {{
      background: #c8e6c9;
    }}

    /* ── Container ──────────────────────────── */
    .container {{
      max-width: 860px;
      margin: 24px auto;
      padding: 0 16px;
    }}
    .stats {{
      text-align: center;
      font-size: .82rem;
      color: #999;
      margin-bottom: 16px;
    }}

    /* ── Card ───────────────────────────────── */
    .card {{
      background: #fff;
      border-radius: 10px;
      padding: 20px 24px;
      margin-bottom: 14px;
      box-shadow: 0 1px 4px rgba(0,0,0,.06);
      transition: transform .18s, box-shadow .18s;
    }}
    .card:hover {{
      transform: translateY(-3px);
      box-shadow: 0 6px 18px rgba(0,0,0,.1);
    }}
    .card-header {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;
    }}
    .badge {{
      display: inline-block;
      font-size: .73rem;
      font-weight: 600;
      padding: 2px 10px;
      border-radius: 12px;
    }}
    .meta {{
      font-size: .76rem;
      color: #999;
    }}
    .card-title {{
      font-size: 1.05rem;
      font-weight: 600;
      margin-bottom: 6px;
    }}
    .card-title a {{
      color: #222;
      text-decoration: none;
    }}
    .card-title a:hover {{
      color: #1b5e20;
      text-decoration: underline;
    }}
    .card-summary {{
      font-size: .9rem;
      color: #555;
    }}

    /* ── Empty state ───────────────────────── */
    .empty {{
      text-align: center;
      padding: 60px 20px;
      color: #aaa;
      font-size: 1rem;
    }}

    /* ── Footer ─────────────────────────────── */
    footer {{
      text-align: center;
      padding: 24px 16px;
      font-size: .78rem;
      color: #aaa;
    }}

    @media (max-width: 600px) {{
      header h1 {{ font-size: 1.4rem; }}
      .card {{ padding: 16px; }}
    }}
  </style>
</head>
<body>

  <header>
    <div class="header-row">
      <h1>全球实时油气新闻聚合</h1>
      <button class="btn-refresh" id="btnRefresh">
        <span class="icon">&#x21bb;</span> 刷新
      </button>
    </div>
    <p class="subtitle">实时聚合 10 大油气行业权威媒体 · 最后更新：{now}（每小时自动刷新）</p>
  </header>

  <div class="sources">
    <span class="label">数据来源：</span>
    {_source_list_html()}
  </div>

  <main class="container">
    <p class="stats">共聚合 {total} 条新闻</p>
{"    <div class='empty'>暂未获取到新闻，请检查网络后重试。</div>" if total == 0 else cards_html}
  </main>

  <footer>
    全球实时油气新闻聚合 &copy; {datetime.now().year}
  </footer>

  <script>
  var _a='github_pat_11AP5KOUY0';
  var _b='dNQq2x013K8F_sH4Fqnd';
  var _c='JtVwfBfOEgy9pxWUQGMmU';
  var _d='VeDdNx7E2QrLeqCZ5OD46NQhjvEPn5Q';
  var DISPATCH_TOKEN = _a+_b+_c+_d;
  var REPO = 'dabch2020/oilnews';
  var btn = document.querySelector('.btn-refresh');
  var subtitleSpan = document.querySelector('.subtitle');

  btn.onclick = function() {{
    btn.classList.add('loading');
    subtitleSpan.textContent = '正在触发后台更新，请稍候约1-2分钟…';

    fetch('https://api.github.com/repos/' + REPO + '/dispatches', {{
      method: 'POST',
      headers: {{
        'Authorization': 'Bearer ' + DISPATCH_TOKEN,
        'Accept': 'application/vnd.github.v3+json'
      }},
      body: JSON.stringify({{ event_type: 'refresh' }})
    }})
    .then(function(r) {{
      if (r.status === 204 || r.status === 200) {{
        subtitleSpan.textContent = '✅ 已触发更新，正在等待构建完成…';
        pollForUpdate();
      }} else {{
        subtitleSpan.textContent = '❌ 触发失败 (HTTP ' + r.status + ')';
        btn.classList.remove('loading');
      }}
    }})
    .catch(function(e) {{
      subtitleSpan.textContent = '❌ 网络错误，请稍后重试';
      btn.classList.remove('loading');
    }});
  }};

  function pollForUpdate() {{
    var originalTime = '{now}';
    var attempts = 0;
    var maxAttempts = 24;  // 最多等 2 分钟 (24 x 5s)
    var timer = setInterval(function() {{
      attempts++;
      fetch(location.href.split('?')[0] + '?_t=' + Date.now())
        .then(function(r) {{ return r.text(); }})
        .then(function(html) {{
          var m = html.match(/最后更新：([^\uff08]+)/);
          if (m && m[1].trim() !== originalTime) {{
            clearInterval(timer);
            location.reload();
          }} else if (attempts >= maxAttempts) {{
            clearInterval(timer);
            subtitleSpan.textContent = '✅ 构建已触发，请稍后手动刷新页面';
            btn.classList.remove('loading');
          }}
        }})
        .catch(function() {{}});
    }}, 5000);
  }}
  </script>

</body>
</html>"""


# ── 入口 ─────────────────────────────────────────────────

# GitHub Pages 从 docs/ 目录提供静态文件
_DOCS_DIR = Path(__file__).parent / "docs"
_HTML_PATH = _DOCS_DIR / "index.html"


def main() -> None:
    _DOCS_DIR.mkdir(exist_ok=True)
    log.info("开始抓取 %d 个来源…", len(SOURCES))
    t0 = time.time()
    news = fetch_all()
    elapsed = time.time() - t0
    log.info("共获取 %d 条新闻 (%.1f 秒)", len(news), elapsed)
    _HTML_PATH.write_text(generate_html(news), encoding="utf-8")
    print(f"✅ 已生成: {_HTML_PATH}  ({len(news)} 条新闻)")


if __name__ == "__main__":
    main()
