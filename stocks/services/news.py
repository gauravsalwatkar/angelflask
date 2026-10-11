"""News angle: world + company headlines from Google News, judged by Claude.

Claude only reads the headlines and scores the *news* for each stock (-1 bearish .. +1 bullish),
including how world events (rates, oil, currency, geopolitics, global tech) affect it. The final
buy / watch / avoid call is made here in code, by combining that news score with the technical
signals we already compute.

Headlines are not verified facts and news scores are an opinion, not investment advice.
"""
import email.utils
import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from django.conf import settings
from django.core.cache import cache

from stocks.analysis import signals
from stocks.models import Instrument

logger = logging.getLogger(__name__)

GOOGLE_NEWS_SEARCH = "https://news.google.com/rss/search"
GOOGLE_NEWS_TOPIC = "https://news.google.com/rss/headlines/section/topic/{topic}"
EDITIONS = {"IN": {"hl": "en-IN", "gl": "IN", "ceid": "IN:en"}, "US": {"hl": "en-US", "gl": "US", "ceid": "US:en"}}

# World / macro news that moves Indian stocks
WORLD_TOPICS = [("BUSINESS", "IN"), ("BUSINESS", "US"), ("WORLD", "US")]
WORLD_QUERIES = [
    "Sensex Nifty market", "crude oil price", "Federal Reserve interest rates", "rupee dollar",
    "FII flows India", "RBI policy", "China economy", "tariffs trade war", "Wall Street stocks",
]
WORLD_HEADLINES = 60
STOCK_HEADLINES = 8
NEWS_DAYS = 7

# Final call thresholds on Claude's news score (-1 .. +1)
BULLISH_NEWS = 0.2
BEARISH_NEWS = -0.3
MIN_TECHNICAL_SIGNALS = 2

HTTP_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (stock-screener news reader)"


class NewsConfigError(RuntimeError):
    pass


# --------------------------------------------------------------------------- headlines

def _parse_feed(xml_text, limit):
    items = []
    for item in ET.fromstring(xml_text).iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        published = item.findtext("pubDate")
        try:
            published = email.utils.parsedate_to_datetime(published).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            pass
        items.append({"title": title, "source": source, "published": published, "link": item.findtext("link")})
        if len(items) >= limit:
            break
    return items


def _get_feed(url, params, limit):
    try:
        response = requests.get(url, params=params, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        return _parse_feed(response.text, limit)
    except (requests.RequestException, ET.ParseError) as exc:
        logger.warning("News feed failed (%s %s): %s", url, params.get("q", ""), exc)
        return []


def search_headlines(query, *, days=NEWS_DAYS, limit=STOCK_HEADLINES, edition="IN"):
    return _get_feed(GOOGLE_NEWS_SEARCH, {"q": f"{query} when:{days}d", **EDITIONS[edition]}, limit)


def _dedupe(items, limit):
    seen, out = set(), []
    for item in sorted(items, key=lambda i: i.get("published") or "", reverse=True):
        key = re.sub(r"\W+", " ", item["title"].lower()).strip()
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out[:limit]


def world_headlines(pool):
    jobs = [pool.submit(_get_feed, GOOGLE_NEWS_TOPIC.format(topic=t), EDITIONS[ed], 20) for t, ed in WORLD_TOPICS]
    jobs += [pool.submit(search_headlines, q, days=2, limit=6, edition="US" if "Wall" in q or "Fed" in q else "IN")
             for q in WORLD_QUERIES]
    return _dedupe([item for job in jobs for item in job.result()], WORLD_HEADLINES)


def _company_name(symbol, exchange):
    """Angel One's 'name' is the bare ticker (TCS-EQ -> TCS), which is what news headlines use."""
    name = Instrument.objects.filter(symbol=symbol, exch_seg=exchange).values_list("name", flat=True).first()
    return name or re.sub(r"-(EQ|BE|SM|ST|BZ)$", "", symbol)


def fetch_all_headlines(stocks):
    """(world headlines, {symbol: headlines}), fetched in parallel."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        stock_jobs = {
            s["symbol"]: pool.submit(search_headlines, f'"{s["name"]}" (share OR stock OR shares OR NSE OR BSE)')
            for s in stocks
        }
        world = world_headlines(pool)
        per_stock = {symbol: _dedupe(job.result(), STOCK_HEADLINES) for symbol, job in stock_jobs.items()}
    return world, per_stock


# --------------------------------------------------------------------------- Claude

ANALYSIS_TOOL = {
    "name": "report_news_analysis",
    "description": "Report the news sentiment for the market and for each stock.",
    "input_schema": {
        "type": "object",
        "properties": {
            "market": {
                "type": "object",
                "properties": {
                    "mood": {"type": "number", "description": "-1 very bearish .. +1 very bullish for Indian equities"},
                    "summary": {"type": "string", "description": "2-3 sentences on what world news means for Indian stocks"},
                    "themes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "theme": {"type": "string"},
                                "impact": {"type": "string", "enum": ["bullish", "bearish", "mixed"]},
                                "sectors": {"type": "array", "items": {"type": "string"},
                                            "description": "Indian sectors affected, e.g. IT, banks, oil & gas, metals, pharma, auto"},
                                "headline": {"type": "integer", "description": "Index of the world headline (W#) it comes from"},
                            },
                            "required": ["theme", "impact", "sectors", "headline"],
                        },
                    },
                },
                "required": ["mood", "summary", "themes"],
            },
            "stocks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "sector": {"type": "string"},
                        "news_score": {"type": "number", "description": "-1 .. +1, company news and world impact combined"},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                        "relevant_headlines": {"type": "integer", "description": "How many of its headlines are really about this company"},
                        "world_impact": {"type": "string", "description": "How the world themes affect this stock, or 'none'"},
                        "reasons": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                        "key_headline": {"type": "string", "description": "The headline that matters most, verbatim, or ''"},
                    },
                    "required": ["symbol", "sector", "news_score", "confidence", "relevant_headlines",
                                 "world_impact", "reasons", "key_headline"],
                },
            },
        },
        "required": ["market", "stocks"],
    },
}

SYSTEM_PROMPT = """You are an equity analyst covering Indian stocks (NSE/BSE). You judge NEWS only, \
not charts. You are given world/market headlines (W#) and, per stock, recent headlines found by a \
news search.

Rules:
- Use only the headlines given. Do not invent events, numbers or dates.
- News searches are noisy: a headline that is not clearly about the company (same word, different \
company, generic market lists) must be ignored and not counted in relevant_headlines.
- Score company news (results, orders, upgrades/downgrades, regulation, fraud, management, deals) \
and the effect of world themes on the company's sector (e.g. lower crude helps paints/airlines/OMCs, \
hurts upstream oil; a weaker rupee helps IT/pharma exporters; US rate cuts help FII flows and banks).
- news_score: -1 strongly negative .. 0 neutral/no relevant news .. +1 strongly positive. With no \
relevant company news, stay within -0.3..+0.3 based only on sector/world effects and use low confidence.
- Reasons are short and concrete, citing the headline they come from.
- Report every stock you are given, using its symbol exactly as written."""


def _client():
    if not settings.ANTHROPIC_API_KEY:
        raise NewsConfigError("Set ANTHROPIC_API_KEY in .env to enable news analysis.")
    import anthropic

    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=180, max_retries=2)


def _build_prompt(world, per_stock, stocks):
    lines = ["WORLD / MARKET HEADLINES"]
    lines += [f"W{i}. [{h['source']}] {h['title']}" for i, h in enumerate(world)] or ["(none)"]
    for s in stocks:
        lines.append(f"\nSTOCK {s['symbol']} ({s['exchange']}, company/ticker: {s['name']})")
        heads = per_stock.get(s["symbol"], [])
        lines += [f"- [{h['source']}, {(h['published'] or '')[:10]}] {h['title']}" for h in heads] or ["- (no headlines found)"]
    return "\n".join(lines)


def analyse(world, per_stock, stocks):
    """One Claude call for the whole scan; returns (analysis dict, usage dict)."""
    response = _client().messages.create(
        model=settings.ANTHROPIC_MODEL,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        tools=[ANALYSIS_TOOL],
        tool_choice={"type": "tool", "name": ANALYSIS_TOOL["name"]},
        messages=[{"role": "user", "content": _build_prompt(world, per_stock, stocks)}],
    )
    block = next(b for b in response.content if b.type == "tool_use")
    usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
    return block.input, usage


# --------------------------------------------------------------------------- scan

def _verdict(news_score, technical):
    if news_score <= BEARISH_NEWS:
        return "avoid"
    if technical and news_score >= BULLISH_NEWS:
        return "buy"
    return "watch"


VERDICT_ORDER = {"buy": 0, "watch": 1, "avoid": 2}


def _technical(symbol):
    info = signals.stock_signals(symbol)
    if info is None:
        return None
    ind = info["indicators"]
    return {
        "symbol": ind["symbol"], "exchange": ind["exchange"], "close": ind.get("close"),
        "target": ind.get("target"), "stop_loss": ind.get("stop_loss"),
        "signals": info["signals"], "signal_count": info["signal_count"],
    }


def pick_stocks(*, technique, limit, symbols, min_price, min_avg_turnover):
    """Stocks to scan: the given symbols, or the current picks of a technique."""
    if symbols:
        found, missing = [], []
        for sym in symbols:
            # NSE (-EQ) first: it has the current candles, BSE history stops earlier
            tech = (None if sym.upper().endswith("-EQ") else _technical(f"{sym}-EQ")) or _technical(sym)
            (found.append(tech) if tech else missing.append(sym))
        return found, missing
    if technique == "top-picks":
        result = signals.top_picks(limit=limit, min_signals=2, min_price=min_price, min_avg_turnover=min_avg_turnover)
    else:
        result = signals.run_technique(technique, limit=limit, min_price=min_price, min_avg_turnover=min_avg_turnover)
    picks = [{
        "symbol": r["symbol"], "exchange": r["exchange"], "close": r["close"], "target": r["target"],
        "stop_loss": r["stop_loss"], "signals": r.get("signals") or [technique], "signal_count": r.get("signal_count", 1),
    } for r in result["results"]]
    return picks, []


def scan(*, technique="top-picks", limit=15, symbols=(), min_price=20.0, min_avg_turnover=5_000_000, refresh=False):
    stocks, missing = pick_stocks(technique=technique, limit=limit, symbols=symbols,
                                  min_price=min_price, min_avg_turnover=min_avg_turnover)
    for s in stocks:
        s["name"] = _company_name(s["symbol"], s["exchange"])

    key = "news:scan:" + hashlib.sha1(json.dumps(
        [settings.ANTHROPIC_MODEL, [s["symbol"] for s in stocks]]).encode()).hexdigest()
    if not refresh and (cached := cache.get(key)):
        return {**cached, "cached": True, "not_found": missing}

    _client()  # fail before fetching anything if there is no key
    world, per_stock = fetch_all_headlines(stocks)
    analysis, usage = analyse(world, per_stock, stocks)

    by_symbol = {a["symbol"].upper(): a for a in analysis.get("stocks", [])}
    rows = []
    for s in stocks:
        a = by_symbol.get(s["symbol"].upper(), {})
        score = max(-1.0, min(1.0, float(a.get("news_score", 0) or 0)))
        technical = s["signal_count"] >= MIN_TECHNICAL_SIGNALS or not symbols  # technique picks already passed
        rows.append({
            "symbol": s["symbol"],
            "verdict": _verdict(score, technical),
            "news_score": round(score, 2),
            "confidence": a.get("confidence", "low"),
            "technical_signals": s["signals"],
            "close": s["close"], "target": s["target"], "stop_loss": s["stop_loss"],
            "sector": a.get("sector", ""),
            "reasons": a.get("reasons", []),
            "world_impact": a.get("world_impact", ""),
            "key_headline": a.get("key_headline", ""),
            "relevant_headlines": a.get("relevant_headlines", 0),
            "headlines_found": len(per_stock.get(s["symbol"], [])),
            "exchange": s["exchange"],
        })
    rows.sort(key=lambda r: (VERDICT_ORDER[r["verdict"]], -r["news_score"]))

    market = analysis.get("market", {})
    for theme in market.get("themes", []):
        i = theme.get("headline")
        theme["source"] = world[i] if isinstance(i, int) and 0 <= i < len(world) else None

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": settings.ANTHROPIC_MODEL,
        "usage": usage,
        "as_of": str(signals.get_snapshot()["date"].max().date()),
        "count": len(rows),
        "universe": len(rows),
        "market": market,
        "results": rows,
        "world_headlines": world,
        "stock_headlines": per_stock,
        "rules": {
            "buy": f"technical signal and news score >= {BULLISH_NEWS}",
            "avoid": f"news score <= {BEARISH_NEWS}, whatever the chart says",
            "watch": "everything else",
        },
    }
    cache.set(key, result, settings.NEWS_CACHE_SECONDS)
    return {**result, "cached": False, "not_found": missing}
