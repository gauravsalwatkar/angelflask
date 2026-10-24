"""Live prices from Angel One's WebSocket 2.0 feed (SmartWebSocketV2), with a quote API fallback.

One background thread per process holds the WebSocket open and subscribes, in QUOTE mode, to every
(exchange, token) pair that `watch` was asked for. Ticks land in an in-memory table. `quotes`
reads that table and, for pairs without a tick in the last STALE_SECONDS (before the open, while
reconnecting, or for a token that never trades), falls back to one REST `getMarketData` FULL call.

Listeners registered with `add_listener` are called with (exchange, token, quote) on every tick,
from the WebSocket thread, so they must be quick and thread safe.
"""
import logging
import threading
import time

from SmartApi.smartWebSocketV2 import SmartWebSocketV2
from django.conf import settings

from stocks.services import smartapi

logger = logging.getLogger(__name__)

EXCHANGE_TYPES = {"NSE": 1, "NFO": 2, "BSE": 3}
EXCHANGE_NAMES = {v: k for k, v in EXCHANGE_TYPES.items()}
QUOTE_MODE = 2
STALE_SECONDS = 20
RECONNECT_SECONDS = 5
REST_BATCH = 50  # getMarketData accepts at most 50 tokens per call
REST_CACHE_SECONDS = 5
HISTORY_STEP_SECONDS = 10   # keep at most one price point per token per 10 s
HISTORY_MAX_POINTS = 2400   # ~6.5 hours at that step

_lock = threading.Lock()
_ticks = {}       # (exchange, token) -> quote dict
_wanted = set()   # (exchange, token) pairs to subscribe to
_subscribed = set()
_listeners = []
_rest_cache = {}  # (exchange, token) -> (fetched_at, quote)
_history = {}     # (exchange, token) -> [(epoch seconds, ltp)], oldest first
_state = {"thread": None, "ws": None, "connected": False, "last_tick": None, "last_error": None}


def _paise(value):
    return round(value / 100, 2) if value else None


def _from_tick(tick):
    return {
        "ltp": _paise(tick.get("last_traded_price")),
        "open": _paise(tick.get("open_price_of_the_day")),
        "high": _paise(tick.get("high_price_of_the_day")),
        "low": _paise(tick.get("low_price_of_the_day")),
        "prev_close": _paise(tick.get("closed_price")),
        "vwap": _paise(tick.get("average_traded_price")),
        "volume": tick.get("volume_trade_for_the_day"),
        "buy_qty": tick.get("total_buy_quantity"),
        "sell_qty": tick.get("total_sell_quantity"),
        "at": time.time(),
        "source": "websocket",
    }


def _from_rest(row):
    return {
        "ltp": row.get("ltp"), "open": row.get("open"), "high": row.get("high"), "low": row.get("low"),
        "prev_close": row.get("close"), "vwap": row.get("avgPrice"), "volume": row.get("tradeVolume"),
        "buy_qty": row.get("totBuyQuan"), "sell_qty": row.get("totSellQuan"),
        "at": time.time(), "source": "quote",
    }


def _record(key, quote):
    """Append to the price history (call with _lock held), thinned to one point per step."""
    if not quote.get("ltp"):
        return
    points = _history.setdefault(key, [])
    if points and quote["at"] - points[-1][0] < HISTORY_STEP_SECONDS:
        points[-1] = (points[-1][0], quote["ltp"])  # keep the step's latest price
    else:
        points.append((quote["at"], quote["ltp"]))
        del points[:-HISTORY_MAX_POINTS]


# ---------- WebSocket thread ----------

def _token_list(pairs):
    by_type = {}
    for exchange, token in pairs:
        by_type.setdefault(EXCHANGE_TYPES[exchange], []).append(token)
    return [{"exchangeType": t, "tokens": tokens} for t, tokens in by_type.items()]


def _subscribe(ws, pairs):
    pairs = set(pairs) - _subscribed
    if not pairs:
        return
    ws.subscribe("live", QUOTE_MODE, _token_list(pairs))
    _subscribed.update(pairs)
    logger.info("Live feed subscribed to %d more tokens (%d total)", len(pairs), len(_subscribed))


def _on_data(_wsapp, tick):
    exchange = EXCHANGE_NAMES.get(tick.get("exchange_type"))
    if not exchange:
        return
    key = (exchange, str(tick.get("token")))
    quote = _from_tick(tick)
    with _lock:
        _ticks[key] = quote
        _state["last_tick"] = quote["at"]
        _record(key, quote)
    for listener in list(_listeners):
        try:
            listener(exchange, key[1], quote)
        except Exception:
            logger.exception("Live feed listener failed")


def _run():
    """Keep one WebSocket open; reconnect (logging in again if needed) whenever it drops."""
    force_login = False
    while True:
        try:
            client = smartapi.get_client(force_login=force_login)
            jwt = client.access_token if str(client.access_token).startswith("Bearer ") else f"Bearer {client.access_token}"
            ws = SmartWebSocketV2(jwt, settings.ANGEL_ONE["API_KEY"], settings.ANGEL_ONE["CLIENT_CODE"],
                                  client.feed_token, max_retry_attempt=0)

            def on_open(_wsapp, ws=ws):
                with _lock:
                    _state["connected"] = True
                    _subscribed.clear()
                    pairs = set(_wanted)
                _subscribe(ws, pairs)
                logger.info("Live feed connected")

            def on_error(*args):
                _state["last_error"] = " ".join(str(a) for a in args)
                logger.warning("Live feed error: %s", _state["last_error"])

            ws.on_open, ws.on_data, ws.on_error = on_open, _on_data, on_error
            ws.on_close = lambda _wsapp: logger.info("Live feed closed")
            _state["ws"] = ws
            ws.connect()  # blocks until the connection closes
            force_login = False
        except Exception as exc:
            _state["last_error"] = str(exc)
            logger.warning("Live feed stopped: %s", exc)
            force_login = True  # an expired session is the usual cause
        _state["connected"] = False
        _state["ws"] = None
        time.sleep(RECONNECT_SECONDS)


def _ensure_running():
    with _lock:
        thread = _state["thread"]
        if thread is None or not thread.is_alive():
            thread = threading.Thread(target=_run, name="angel-live-feed", daemon=True)
            _state["thread"] = thread
            thread.start()


# ---------- public API ----------

def watch(pairs):
    """Subscribe to these (exchange, token) pairs, starting the feed if it is not running."""
    pairs = {(e, str(t)) for e, t in pairs if e in EXCHANGE_TYPES}
    with _lock:
        new = pairs - _wanted
        _wanted.update(pairs)
    _ensure_running()
    ws = _state["ws"]
    if new and ws is not None and _state["connected"]:
        try:
            _subscribe(ws, new)
        except Exception as exc:
            logger.warning("Live feed subscribe failed: %s", exc)


def add_listener(fn):
    with _lock:
        if fn not in _listeners:
            _listeners.append(fn)


def _rest_quotes(pairs):
    now = time.time()
    result, missing = {}, []
    for pair in pairs:
        cached = _rest_cache.get(pair)
        if cached and now - cached[0] < REST_CACHE_SECONDS:
            result[pair] = cached[1]
        else:
            missing.append(pair)
    for i in range(0, len(missing), REST_BATCH):
        batch = missing[i:i + REST_BATCH]
        tokens = {}
        for exchange, token in batch:
            tokens.setdefault(exchange, []).append(token)
        try:
            response = smartapi.get_client().getMarketData("FULL", tokens)
        except Exception as exc:
            logger.warning("Quote fallback failed: %s", exc)
            continue
        for row in ((response or {}).get("data") or {}).get("fetched") or []:
            pair = (row.get("exchange"), str(row.get("symbolToken")))
            quote = _from_rest(row)
            _rest_cache[pair] = (now, quote)
            result[pair] = quote
            with _lock:
                _record(pair, quote)
    return result


def quotes(pairs):
    """{(exchange, token): quote} - live ticks where fresh, otherwise the REST quote."""
    pairs = {(e, str(t)) for e, t in pairs}
    watch(pairs)
    now = time.time()
    with _lock:
        result = {p: _ticks[p] for p in pairs if p in _ticks and now - _ticks[p]["at"] < STALE_SECONDS}
    stale = [p for p in pairs if p not in result]
    if stale:
        result.update(_rest_quotes(stale))
    return result


def history(pair, since=None):
    """[(epoch seconds, ltp)] recorded for the pair, optionally only points after `since`."""
    with _lock:
        points = list(_history.get((pair[0], str(pair[1])), []))
    return [p for p in points if since is None or p[0] >= since]


def seed_history(pair, points):
    """Put earlier points (e.g. from 1-minute candles) in front of what the feed recorded."""
    key = (pair[0], str(pair[1]))
    with _lock:
        recorded = _history.get(key, [])
        first = recorded[0][0] if recorded else float("inf")
        _history[key] = ([p for p in sorted(points) if p[0] < first] + recorded)[-HISTORY_MAX_POINTS:]


def status():
    last = _state["last_tick"]
    return {
        "connected": _state["connected"],
        "subscribed": len(_subscribed),
        "seconds_since_last_tick": round(time.time() - last, 1) if last else None,
        "last_error": _state["last_error"],
    }
