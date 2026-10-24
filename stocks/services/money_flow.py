"""Intraday money flow: which stocks money is going into (or out of) right now.

Cash market, for the `count` most liquid stocks (by the stored 20-day turnover), quoted on NSE
where an "-EQ" contract exists (that is where most of the trading happens), else on the stored
exchange. The instruments table holds BSE shares and index options only, so the NSE tokens come
from Angel One's scrip master, saved in DATA_DIR and refreshed weekly:

* traded value today = volume x VWAP (the average traded price), in crores.
* relative turnover = today's traded value vs the stock's normal day (average turnover of its last
  20 sessions), scaled by how much of the session has passed, so 11:00 is not compared with a full
  day. 2.0 = twice the usual money for this time of day. The NSE baselines come from 20 daily
  candles per stock, fetched once a day in a background thread (about a second per stock) and
  saved in DATA_DIR, so relative turnover fills in over the first minutes.
* direction: up from the previous close and at or above VWAP = buying (buyers paid up all day);
  down and below VWAP = selling; anything else is mixed. Heavy = relative turnover >= HEAVY.
  Pending buy vs sell quantity in the order book is shown, not used: it is easy to spoof.

F&O, from Angel One's OI build-up lists for near-month stock futures:

* long build-up (price up, open interest up): fresh buying.
* short covering (price up, OI down): sellers closing out.
* short build-up (price down, OI up): fresh selling.
* long unwinding (price down, OI down): buyers closing out.

Volume is not spread evenly over the day (the open and close are busiest), so relative turnover
reads high in the first hour. These are readings of the tape, not investment advice.
"""
import json
import logging
import re
import threading
import time
from datetime import date, time as dtime, timedelta

import pandas as pd
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.utils import timezone

from stocks.analysis import flow_strategy, signals
from stocks.models import Instrument
from stocks.services import historic, instruments, live_feed, smartapi

logger = logging.getLogger(__name__)

SESSION_OPEN, SESSION_CLOSE = dtime(9, 15), dtime(15, 30)
SESSION_MINUTES = 375
BASELINE_SESSIONS = 20
BASELINE_CALENDAR_DAYS = 45   # enough calendar days to hold 20 sessions
HEAVY = 1.5
CRORE = 1e7
MIN_PRICE, MIN_AVG_TURNOVER = 20.0, 5_000_000
FNO_TYPES = {
    "long_buildup": "Long Built Up",
    "short_covering": "Short Covering",
    "short_buildup": "Short Built Up",
    "long_unwinding": "Long Unwinding",
}
FNO_CACHE_SECONDS = 60
FNO_ROWS = 15
RATE_LIMIT_PAUSE = 60
NSE_TOKENS_MAX_AGE_DAYS = 7

DISCLAIMER = "A reading of today's trading, not investment advice. No orders are placed."


def _market_phase(now):
    t = now.time()
    if now.weekday() >= 5 or t < SESSION_OPEN or t >= SESSION_CLOSE:
        return "closed"
    return "open"


def _session_fraction(now, phase):
    """Share of the session that has passed; a closed market shows the last full session."""
    if phase == "closed":
        return 1.0
    minutes = (now - now.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute, second=0, microsecond=0)).seconds / 60
    return min(max(minutes / SESSION_MINUTES, 0.02), 1.0)


def _pct(a, b):
    return round((a / b - 1) * 100, 2) if a and b else None


# ---------- universe ----------

_nse_lock = threading.Lock()
_nse = {"date": None, "tokens": {}, "thread": None, "retry_at": 0}
NSE_RETRY_SECONDS = 10 * 60


def _nse_tokens_path():
    return settings.DATA_DIR / "nse_equity_tokens.json"


def _download_nse_tokens():
    """Background thread: the scrip master is large (minutes to download), so never block a request on it."""
    try:
        items = instruments._download_scrip_master()
        tokens = {i["symbol"][:-3]: str(i["token"]) for i in items
                  if i.get("exch_seg") == "NSE" and str(i.get("symbol", "")).endswith("-EQ")}
        today = timezone.localdate()
        path = _nse_tokens_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"date": str(today), "tokens": tokens}), encoding="utf-8")
        with _nse_lock:
            _nse.update(date=today, tokens=tokens)
        logger.info("Saved %d NSE equity tokens", len(tokens))
    except Exception as exc:  # keep any old copy; try again later
        logger.warning("Scrip master download for NSE tokens failed: %s", exc)
        with _nse_lock:
            _nse["retry_at"] = time.time() + NSE_RETRY_SECONDS
    finally:
        with _nse_lock:
            _nse["thread"] = None


def _nse_equity_tokens():
    """({symbol: NSE "-EQ" token}, loading) - the saved copy, refreshed in the background once a week old."""
    with _nse_lock:
        if _nse["date"] is None:
            try:
                data = json.loads(_nse_tokens_path().read_text(encoding="utf-8"))
                _nse.update(date=date.fromisoformat(data["date"]), tokens=data["tokens"])
            except (OSError, ValueError, KeyError):
                pass
        stale = not _nse["date"] or (timezone.localdate() - _nse["date"]).days >= NSE_TOKENS_MAX_AGE_DAYS
        if stale and _nse["thread"] is None and time.time() >= _nse["retry_at"]:
            _nse["thread"] = threading.Thread(target=_download_nse_tokens, name="nse-equity-tokens", daemon=True)
            _nse["thread"].start()
        return _nse["tokens"], _nse["thread"] is not None


def _universe(count):
    """(The `count` most liquid stored stocks, each mapped to its NSE "-EQ" token where there is one,
    whether the NSE token list is still downloading)."""
    nse, loading = _nse_equity_tokens()
    key = f"money-flow-universe:{timezone.localdate()}:{count}:{len(nse)}"
    rows = cache.get(key)
    if rows is not None:
        return rows, loading
    s, _ = signals._universe(min_price=MIN_PRICE, min_avg_turnover=MIN_AVG_TURNOVER)
    s = s.sort_values("avg_turnover", ascending=False).head(count)
    rows = []
    for r in s.itertuples():
        token = nse.get(r.symbol)
        if token:
            rows.append({"symbol": r.symbol, "exchange": "NSE", "token": str(token), "stored_turnover": None})
        else:  # no NSE contract: quote the stored exchange and use its stored turnover as the baseline
            rows.append({"symbol": r.symbol, "exchange": r.exchange, "token": str(r.token),
                         "stored_turnover": float(r.avg_turnover)})
    cache.set(key, rows, 60 * 60)
    return rows, loading


# ---------- NSE turnover baselines (background) ----------

_baseline_lock = threading.Lock()
_baseline = {"date": None, "values": {}, "failed": set(), "pending": set(), "thread": None}


def _baseline_path():
    return settings.DATA_DIR / "money_flow_baseline.json"


def _load_baseline(today):
    """Switch to today's baselines, reading what an earlier run saved (call with the lock held)."""
    if _baseline["date"] == today:
        return
    values = {}
    try:
        data = json.loads(_baseline_path().read_text(encoding="utf-8"))
        if data.get("date") == str(today):
            values = data["values"]
    except (OSError, ValueError, KeyError):
        pass
    _baseline.update(date=today, values=values, failed=set(), pending=set())


def _save_baseline():
    with _baseline_lock:
        data = {"date": str(_baseline["date"]), "values": dict(_baseline["values"])}
    path = _baseline_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _fetch_baseline(client, token, today):
    """Average daily turnover of the last BASELINE_SESSIONS completed sessions, or None."""
    inst = Instrument(token=token, exch_seg="NSE", symbol=token)  # not saved: only the fetch reads it
    start = today - timedelta(days=BASELINE_CALENDAR_DAYS)
    df = historic.fetch_candles(client, inst, f"{start} 00:00", f"{today} 15:30")
    if df is None or df.empty:
        return None
    df = df[pd.to_datetime(df["date"]).dt.date < today].tail(BASELINE_SESSIONS)  # today's candle is partial
    return float((df["close"] * df["volume"]).mean()) if len(df) else None


def _build_baselines():
    """Background thread: fetch every pending token's baseline, saving as it goes."""
    try:
        client = smartapi.get_client()
        done = 0
        while True:
            with _baseline_lock:
                if not _baseline["pending"]:
                    break
                token, today = _baseline["pending"].pop(), _baseline["date"]
            try:
                value = _fetch_baseline(client, token, today)
            except historic.RateLimitExceeded:
                logger.warning("Money flow baselines rate limited; pausing %ds", RATE_LIMIT_PAUSE)
                with _baseline_lock:
                    _baseline["pending"].add(token)
                time.sleep(RATE_LIMIT_PAUSE)
                continue
            except Exception:
                logger.exception("Money flow baseline for %s failed", token)
                value = None
            with _baseline_lock:
                if _baseline["date"] != today:  # the day rolled over; start again
                    continue
                if value:
                    _baseline["values"][token] = value
                else:
                    _baseline["failed"].add(token)
            done += 1
            if done % 10 == 0:
                _save_baseline()
        _save_baseline()
    except Exception:
        logger.exception("Money flow baselines stopped")
    finally:
        with _baseline_lock:
            _baseline["thread"] = None
        connection.close()


def _baselines(tokens):
    """{token: average daily turnover} known so far, plus progress; starts fetching the rest."""
    with _baseline_lock:
        _load_baseline(timezone.localdate())
        known = _baseline["values"]
        missing = {t for t in tokens if t not in known and t not in _baseline["failed"]}
        _baseline["pending"] |= missing
        if _baseline["pending"] and _baseline["thread"] is None:
            _baseline["thread"] = threading.Thread(target=_build_baselines, name="money-flow-baselines", daemon=True)
            _baseline["thread"].start()
        values = {t: known[t] for t in tokens if t in known}
        progress = {"ready": len(values), "total": len(tokens),
                    "failed": sum(t in _baseline["failed"] for t in tokens),
                    "loading": _baseline["thread"] is not None}
    return values, progress


# ---------- cash market ----------

def _flow(change, ltp, vwap, rel):
    up = change is not None and change > 0 and vwap and ltp >= vwap
    down = change is not None and change < 0 and vwap and ltp < vwap
    heavy = rel is not None and rel >= HEAVY
    if up:
        return ("heavy_buying" if heavy else "buying"), 1
    if down:
        return ("heavy_selling" if heavy else "selling"), -1
    return "mixed", 0


def _why(r):
    parts = []
    if r["change_pct"] is not None:
        parts.append(f"{r['change_pct']:+.2f}% today")
    if r["vs_vwap_pct"] is not None:
        parts.append(f"{abs(r['vs_vwap_pct']):.2f}% {'above' if r['vs_vwap_pct'] >= 0 else 'below'} VWAP")
    if r["relative_turnover"] is not None:
        parts.append(f"{r['relative_turnover']:.1f}x its usual money for this time of day")
    else:
        parts.append("usual turnover still loading")
    return ", ".join(parts)


def equity_flow(count=100):
    now = timezone.localtime()
    phase = _market_phase(now)
    fraction = _session_fraction(now, phase)
    universe, nse_loading = _universe(count)
    baseline, progress = _baselines([u["token"] for u in universe if u["exchange"] == "NSE"])
    quotes = live_feed.quotes({(u["exchange"], u["token"]) for u in universe})

    rows, no_price = [], []
    for u in universe:
        q = quotes.get((u["exchange"], u["token"]), {})
        ltp, vwap, volume = q.get("ltp"), q.get("vwap"), q.get("volume")
        if not ltp or not volume:
            no_price.append(u["symbol"])
            continue
        value = volume * (vwap or ltp)
        normal = u["stored_turnover"] or baseline.get(u["token"])
        rel = round(value / (normal * fraction), 2) if normal else None
        buy_qty, sell_qty = q.get("buy_qty"), q.get("sell_qty")
        row = {
            "symbol": u["symbol"], "exchange": u["exchange"], "token": u["token"],
            "ltp": ltp, "change_pct": _pct(ltp, q.get("prev_close")), "vwap": vwap,
            "vs_vwap_pct": _pct(ltp, vwap), "open": q.get("open"), "high": q.get("high"), "low": q.get("low"),
            "volume": volume, "value_cr": round(value / CRORE, 2),
            "normal_value_cr": round(normal / CRORE, 2) if normal else None,
            "relative_turnover": rel,
            "book_buy_pct": round(buy_qty / (buy_qty + sell_qty) * 100, 1) if buy_qty and sell_qty else None,
        }
        row["flow"], row["direction"] = _flow(row["change_pct"], ltp, vwap, rel)
        row["why"] = _why(row)
        rows.append(row)

    by_value = lambda r: -r["value_cr"]
    buying = sorted((r for r in rows if r["direction"] > 0), key=by_value)
    selling = sorted((r for r in rows if r["direction"] < 0), key=by_value)
    unusual = sorted((r for r in rows if r["relative_turnover"]), key=lambda r: -r["relative_turnover"])[:15]
    total = lambda rs: round(sum(r["value_cr"] for r in rs), 2)
    return {
        "time": now.strftime("%H:%M:%S"), "market": phase,
        "session_elapsed_pct": round(fraction * 100, 1),
        "counts": {k: sum(r["flow"] == k for r in rows)
                   for k in ("heavy_buying", "buying", "mixed", "selling", "heavy_selling")},
        "value_cr": {"buying": total(buying), "selling": total(selling), "all": total(rows)},
        "buying": buying, "selling": selling, "unusual": unusual,
        "stocks": len(universe), "no_price": no_price, "baselines": progress, "nse_tokens_loading": nse_loading,
        "feed": live_feed.status(),
    }


# ---------- F&O OI build-up ----------

def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _underlying(trading_symbol):
    return re.sub(r"\d{2}[A-Z]{3}(\d{2})?FUT$", "", trading_symbol or "")


def fno_buildup():
    """Angel One's near-month OI build-up lists, cached for FNO_CACHE_SECONDS."""
    cached = cache.get("money-flow-fno")
    if cached is not None:
        return cached
    client = smartapi.get_client()
    out, errors = {}, []
    for name, datatype in FNO_TYPES.items():
        try:
            response = client.oIBuildup({"expirytype": "NEAR", "datatype": datatype})
            time.sleep(0.4)  # SmartAPI rate limit
        except Exception as exc:
            response = {"message": str(exc)}
        data = (response or {}).get("data")
        if not (response or {}).get("status") or not isinstance(data, list):
            errors.append(f"{datatype}: {(response or {}).get('message') or 'no data'}")
            out[name] = []
            continue
        out[name] = [{
            "symbol": _underlying(r.get("tradingSymbol")), "contract": r.get("tradingSymbol"),
            "ltp": _num(r.get("ltp")), "change_pct": _num(r.get("percentChange")),
            "oi": _num(r.get("opnInterest")), "oi_change": _num(r.get("netChangeOpnInterest")),
        } for r in data[:FNO_ROWS]]
    result = {**out, "errors": errors, "fetched_at": timezone.localtime().strftime("%H:%M:%S")}
    if not errors:
        cache.set("money-flow-fno", result, FNO_CACHE_SECONDS)
    return result


def money_flow(count=100, fno=True):
    result = equity_flow(count)
    if fno:
        try:
            result["fno"] = fno_buildup()
        except Exception as exc:  # login / network: the cash-market part still works
            logger.warning("OI build-up failed: %s", exc)
            result["fno"] = {**{k: [] for k in FNO_TYPES}, "errors": [str(exc)]}
    try:
        result["strategy"] = flow_strategy.live(result)
    except Exception as exc:  # the flow itself still works
        logger.exception("Money-flow strategy signals failed")
        result["strategy"] = {"error": str(exc), "signals": []}
    result["method"] = (
        f"Top {result['stocks']} liquid stocks. Buying = up today and above VWAP; selling = down and below VWAP. "
        f"Heavy = at least {HEAVY}x the stock's usual turnover for this time of day.")
    result["disclaimer"] = DISCLAIMER
    return result
