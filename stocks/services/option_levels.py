"""Intraday entry, stop loss and target levels for NIFTY / BANKNIFTY options.

Two rules from the "Simple maths formula book" sheet:

* square (15-minute sheet): strikes come from the close of the index's first 15-minute candle
  rounded to 100 - CE 100 below, PE 100 above. For each option, the entry is the next perfect
  square above the high of its first 15-minute candle, the stop loss is the square below the
  entry, and the targets are entry + 10 followed by the next five squares.
* bounce_back: at the ATM strike (the first-hour index close rounded to 100), from the low L of
  the option's first hour: buy above 1.5 L, stop loss 1.2 L, targets 1.8 L, 2 L and 2.5 L.

Each level set is then checked against the option's later 15-minute candles that day: was the
entry reached, and after that the stop loss or which targets. Checking starts with the candle
after the entry, and a candle touching both the stop and a target counts as the stop.
"""
import logging
import math
import time
from datetime import datetime, timedelta

from django.core.cache import cache
from django.utils import timezone

from stocks.models import Instrument
from stocks.services import smartapi
from stocks.services.historic import RATE_LIMIT_MARKER

logger = logging.getLogger(__name__)

INDEX_TOKENS = {"NIFTY": "99926000", "BANKNIFTY": "99926009"}
STRIKE_STEP = 100
INTERVAL = "FIFTEEN_MINUTE"
CANDLE_MINUTES = 15
FIRST_HOUR_CANDLES = 4
CACHE_SECONDS = 60


class LevelsError(RuntimeError):
    pass


def mround(value, step=STRIKE_STEP):
    """Excel MROUND for positive numbers: nearest multiple of `step`, halves rounded up."""
    return int(value / step + 0.5) * step


def square_levels(high):
    root = math.isqrt(int(high)) + 1  # INT(SQRT(high)) + 1
    entry = root ** 2
    return {
        "entry": entry,
        "stop_loss": (root - 1) ** 2,
        "targets": [entry + 10] + [(root + k) ** 2 for k in range(1, 6)],
    }


def bounce_levels(low):
    return {
        "entry": round(low * 1.5, 2),
        "stop_loss": round(low * 1.2, 2),
        "targets": [round(low * m, 2) for m in (1.8, 2, 2.5)],
    }


def _candles(client, exchange, token, day):
    """The day's 15-minute candles, each flagged `complete` once its 15 minutes are over."""
    params = {"exchange": exchange, "symboltoken": token, "interval": INTERVAL,
              "fromdate": f"{day} 09:15", "todate": f"{day} 15:30"}
    for attempt in range(3):
        try:
            response = client.getCandleData(params)
            message = str((response or {}).get("message", ""))
        except Exception as exc:
            response, message = None, str(exc)
        if RATE_LIMIT_MARKER not in message.lower():
            break
        time.sleep(2 + attempt * 3)
    else:
        raise LevelsError("Angel One rate limit exceeded; try again in a minute")
    time.sleep(0.4)  # stay under SmartAPI's rate limit across the several calls per request

    if not response or not response.get("status"):
        raise LevelsError(f"No candles for {exchange} token {token}: {message or 'empty response'}")
    now = timezone.localtime()
    rows = []
    for ts, o, h, l, c, _volume in response.get("data") or []:
        start = datetime.fromisoformat(ts)
        rows.append({"time": start.strftime("%H:%M"), "open": o, "high": h, "low": l, "close": c,
                     "complete": start + timedelta(minutes=CANDLE_MINUTES) <= now})
    return rows


def _option(underlying, strike, kind, day):
    """Nearest-expiry stored contract for the strike; scrip master strikes are in paise."""
    return (
        Instrument.objects.filter(exch_seg="NFO", instrumenttype="OPTIDX", name=underlying,
                                  strike=strike * 100, expiry__gte=day, symbol__endswith=kind)
        .order_by("expiry").first()
    )


def _outcome(levels, later):
    """How the day played out after the reference candles."""
    entry, stop, targets = levels["entry"], levels["stop_loss"], levels["targets"]
    result = {"status": "waiting", "entry_time": None, "targets_hit": 0, "stop_time": None}
    entered = False
    for c in later:
        if not entered:
            if c["high"] >= entry:
                entered = True
                result.update(status="open", entry_time=c["time"])
            continue
        if c["low"] <= stop:
            result.update(status="stop_loss", stop_time=c["time"])
            break
        hit = sum(c["high"] >= t for t in targets)
        if hit > result["targets_hit"]:
            result["targets_hit"] = hit
            result["status"] = "all_targets" if hit == len(targets) else "target"
            if hit == len(targets):
                break
    return result


def _leg(client, candle_cache, underlying, strike, kind, day, ref_count, rule):
    leg = {"type": kind, "strike": strike}
    inst = _option(underlying, strike, kind, day)
    if inst is None:
        return {**leg, "error": "No contract stored for this strike - sync the option contracts"}
    leg.update(symbol=inst.symbol, token=inst.token, expiry=inst.expiry, lotsize=inst.lotsize)

    if inst.token not in candle_cache:
        candle_cache[inst.token] = _candles(client, "NFO", inst.token, day)
    candles = candle_cache[inst.token]
    ref = candles[:ref_count]
    if len(ref) < ref_count or not ref[-1]["complete"]:
        return {**leg, "error": "The option's reference candles are not complete yet"}

    if rule == "square":
        reference = ref[0]["high"]
        levels = square_levels(reference)
    else:
        reference = min(c["low"] for c in ref)
        levels = bounce_levels(reference)
    return {**leg, "reference": reference, **levels, "last_price": candles[-1]["close"],
            **_outcome(levels, candles[ref_count:])}


def option_levels(underlying="NIFTY", day=None):
    """Levels for both rules on `day` (default today), with how each played out so far."""
    day = day or timezone.localdate()
    key = f"option-levels:{underlying}:{day}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    client = smartapi.get_client()
    index = _candles(client, "NSE", INDEX_TOKENS[underlying], day)
    if not index:
        raise LevelsError(f"No {underlying} candles on {day} - market holiday, weekend or not open yet")

    candle_cache = {}
    result = {
        "underlying": underlying,
        "date": day,
        "index": {"last": index[-1]["close"], "last_time": index[-1]["time"]},
        "square": {"available_from": "09:30"},
        "bounce_back": {"available_from": "10:15"},
    }
    if index[0]["complete"]:
        base = mround(index[0]["close"])
        result["index"]["first_15m_close"] = index[0]["close"]
        result["square"] = {"base_strike": base, "legs": [
            _leg(client, candle_cache, underlying, base - STRIKE_STEP, "CE", day, 1, "square"),
            _leg(client, candle_cache, underlying, base + STRIKE_STEP, "PE", day, 1, "square"),
        ]}
    if len(index) >= FIRST_HOUR_CANDLES and index[FIRST_HOUR_CANDLES - 1]["complete"]:
        atm = mround(index[FIRST_HOUR_CANDLES - 1]["close"])
        result["index"]["first_hour_close"] = index[FIRST_HOUR_CANDLES - 1]["close"]
        result["bounce_back"] = {"base_strike": atm, "legs": [
            _leg(client, candle_cache, underlying, atm, kind, day, FIRST_HOUR_CANDLES, "bounce_back")
            for kind in ("CE", "PE")
        ]}

    cache.set(key, result, CACHE_SECONDS)
    return result
