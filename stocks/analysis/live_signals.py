"""Live buy / add-position suggestions from the WebSocket feed (see stocks.services.live_feed).

Equity: today's recommendations (stocks.analysis.recommend, planned on the last daily candle) are
re-checked against the live price before buying:

* AVOID when the price is already below the stop loss or at the target.
* BUY when every required check passes: after 09:30 (opening swings settled), not above
  `max_buy_price` (the highest price that still keeps MIN_REWARD_TO_RISK), at or above today's VWAP
  (average traded price) and at or above today's open. More pending buyers than sellers is shown
  but not required.
* WAIT otherwise, with the failed checks saying what is missing.

Every BUY is sized by stocks.analysis.risk: shares that keep the loss at the stop within
MAX_LOSS_PER_TRADE and the cost within TRADING_CAPITAL; when not even one share fits, it is WAIT.
Each pick also carries the time machine's odds (stocks.analysis.simulate) from its daily plan.

Options: the legs from stocks.services.option_levels are tracked tick by tick (entry crossed, targets
hit, stop hit), seeded from the 15-minute candle outcome so a restart mid-day keeps what happened.

* WAIT below the entry; BUY once the entry is crossed while the price is still near it.
* HOLD when it is in the trade but too far above the entry to buy fresh.
* ADD after target 1 has been hit, on a pullback into the zone between the entry and target 1,
  while the index still agrees (above its reference close for a CE, below it for a PE). The stop for
  the whole position moves up to the entry, so the add risks no more than the first lot's profit.
* BOOK once every target is hit, EXIT after the stop loss.
* BUY / ADD are sized in lots by stocks.analysis.risk (SKIP when even one lot risks too much), only
  until risk.LAST_ENTRY; from risk.SQUARE_OFF a leg still in the trade is EXIT, so nothing is
  carried past the close.

All of this is mechanical rules on prices, not investment advice.
"""
import logging
import threading
import time
from datetime import datetime, time as dtime

from django.core.cache import cache
from django.utils import timezone

from stocks.analysis import recommend, risk
from stocks.services import live_feed, option_levels, smartapi

logger = logging.getLogger(__name__)

MARKET_OPEN, SETTLED, MARKET_CLOSE = dtime(9, 15), dtime(9, 30), dtime(15, 30)
# The plan itself is 1.5 (stop 2 ATR, target 3 ATR); 1.0 allows buying up to 0.5 ATR above the plan.
MIN_REWARD_TO_RISK = 1.0
EQUITY_PICKS_CACHE_SECONDS = 60 * 60  # the plan uses daily candles, so it rarely changes
NEAR_ENTRY_FRACTION = 0.25  # "still near the entry" = within this share of the entry -> target 1 gap

DISCLAIMER = "Mechanical rules on live prices; not investment advice. Check the price yourself before you trade."


def _market_phase(now):
    t = now.time()
    if now.weekday() >= 5 or t < MARKET_OPEN or t >= MARKET_CLOSE:
        return "closed"
    return "opening" if t < SETTLED else "open"


def _pct(a, b):
    return round((a / b - 1) * 100, 2) if a and b else None


# ---------- equity ----------

def _equity_picks(count):
    key = f"live-equity-picks:{timezone.localdate()}:{count}"
    picks = cache.get(key)
    if picks is None:
        picks = recommend.recommend(count=count, simulate=True)
        cache.set(key, picks, EQUITY_PICKS_CACHE_SECONDS)
    return picks


_buy_since = {}  # (day, symbol) -> time the stock first turned BUY today


def _check(name, ok, detail, required=True):
    return {"name": name, "ok": bool(ok), "detail": detail, "required": required}


def _equity_checks(pick, q, phase, position):
    """The conditions a BUY needs, each passed or failed, with the live numbers behind it."""
    ltp, stop, target, max_buy = q.get("ltp"), pick["stop_loss"], pick["target"], pick["max_buy_price"]
    vwap, day_open = q.get("vwap"), q.get("open")
    buy_qty, sell_qty = q.get("buy_qty"), q.get("sell_qty")
    return [
        _check("Market settled", phase == "open",
               {"closed": "Market is closed (live 09:15-15:30)",
                "opening": "First 15 minutes: wait for the opening swings to settle (from 09:30)"}.get(
                   phase, "After 09:30, opening swings are over")),
        _check("Above stop loss", ltp and ltp > stop, f"Price {ltp} vs stop loss {stop}"),
        _check("Room to target", ltp and ltp < target, f"Price {ltp} vs target {target}"),
        _check("Not chasing", ltp and ltp <= max_buy,
               f"Price {ltp} vs max buy price {max_buy} (reward:risk at least {MIN_REWARD_TO_RISK})"),
        _check("Above VWAP", ltp and vwap and ltp >= vwap,
               f"Price {ltp} vs today's average traded price {vwap}: buyers in control when above"),
        _check("Green day", ltp and day_open and ltp >= day_open, f"Price {ltp} vs today's open {day_open}"),
        _check("Fits your risk", *risk.fits_detail(position)),
        _check("More buyers than sellers", buy_qty and sell_qty and buy_qty >= sell_qty,
               f"Pending buy qty {buy_qty:,.0f} vs sell qty {sell_qty:,.0f}" if buy_qty and sell_qty
               else "Order book quantities not available", required=False),
    ]


def _equity_action(checks, ltp):
    failed = {c["name"] for c in checks if c["required"] and not c["ok"]}
    if not ltp:
        return "WAIT"
    if failed & {"Above stop loss", "Room to target"}:
        return "AVOID"
    return "WAIT" if failed else "BUY"


def equity_suggestions(count=5):
    now = timezone.localtime()
    phase = _market_phase(now)
    plan = _equity_picks(count)
    picks = plan["recommendations"]
    quotes = live_feed.quotes({(p["exchange"], p["token"]) for p in picks})
    rows = []
    for p in picks:
        q = quotes.get((p["exchange"], str(p["token"])), {})
        ltp = q.get("ltp")
        # Highest buy price that still gives MIN_REWARD_TO_RISK: (target - x) = R * (x - stop)
        max_buy = round((p["target"] + MIN_REWARD_TO_RISK * p["stop_loss"]) / (1 + MIN_REWARD_TO_RISK), 2)
        p = {**p, "max_buy_price": max_buy}
        position = risk.size(ltp, p["stop_loss"]) if ltp else None
        checks = _equity_checks(p, q, phase, position)
        action = _equity_action(checks, ltp)
        key = (str(now.date()), p["symbol"])
        if action == "BUY":
            _buy_since.setdefault(key, now.strftime("%H:%M:%S"))
        else:
            _buy_since.pop(key, None)
        rows.append({
            "symbol": p["symbol"], "exchange": p["exchange"], "token": p["token"],
            "action": action, "buy_since": _buy_since.get(key), "checks": checks,
            "why_picked": p["why"], "risks": p["risks"], "signals": p["signals"],
            "planned_buy": p["buy_price"], "max_buy_price": max_buy,
            "stop_loss": p["stop_loss"], "target": p["target"],
            "live_reward_to_risk": round((p["target"] - ltp) / (ltp - p["stop_loss"]), 2)
            if ltp and ltp > p["stop_loss"] else None,
            "ltp": ltp, "change_pct": _pct(ltp, q.get("prev_close")),
            "gap_pct": _pct(q.get("open"), q.get("prev_close")),
            "vwap": q.get("vwap"), "open": q.get("open"), "high": q.get("high"), "low": q.get("low"),
            "price_source": q.get("source"),
            "position": position, "simulation": p.get("simulation"),
        })
    order = {"BUY": 0, "WAIT": 1, "AVOID": 2}
    # BUY first, then the stocks closest to qualifying (fewest failed conditions).
    rows.sort(key=lambda r: (order[r["action"]], sum(not c["ok"] for c in r["checks"] if c["required"])))
    return {
        "time": now.strftime("%H:%M:%S"), "market": phase, "plan_as_of": plan["as_of"],
        "counts": {a: sum(r["action"] == a for r in rows) for a in order},
        "suggestions": rows, "message": plan.get("message"),
        "feed": live_feed.status(), "disclaimer": DISCLAIMER,
    }


# ---------- options ----------

_legs_lock = threading.Lock()
_legs = {}  # (day, rule, token) -> tracking state


def _track_tick(exchange, token, quote):
    """Live feed listener: move each tracked leg through entry -> targets / stop on every tick."""
    ltp = quote.get("ltp")
    if exchange != "NFO" or not ltp:
        return
    now = timezone.localtime().strftime("%H:%M:%S")
    with _legs_lock:
        for (day, _rule, tok), s in _legs.items():
            if tok != token or s["stopped_at"] or s["targets_hit"] == len(s["targets"]):
                continue
            if not s["entered_at"]:
                if ltp >= s["entry"]:
                    s["entered_at"] = now
                continue
            s["high_since_entry"] = max(s["high_since_entry"] or ltp, ltp)
            if ltp <= _trail_stop(s):
                s["stopped_at"] = now
                continue
            s["targets_hit"] = max(s["targets_hit"], sum(ltp >= t for t in s["targets"]))


live_feed.add_listener(_track_tick)


def _trail_stop(s):
    """Original stop until target 1; then the entry; after target k (k >= 2), target k - 1."""
    k = s["targets_hit"]
    if k == 0:
        return s["stop_loss"]
    return s["entry"] if k == 1 else s["targets"][k - 2]


def _leg_state(day, rule, leg):
    key = (str(day), rule, str(leg["token"]))
    with _legs_lock:
        s = _legs.get(key)
        if s is None:  # seed from the 15-minute candle outcome
            s = _legs[key] = {
                "entry": leg["entry"], "stop_loss": leg["stop_loss"], "targets": leg["targets"],
                "entered_at": leg.get("entry_time"), "targets_hit": leg.get("targets_hit", 0),
                "stopped_at": leg.get("stop_time"), "high_since_entry": None,
            }
        else:  # candles can catch a move made while the server was down
            s["targets_hit"] = max(s["targets_hit"], leg.get("targets_hit", 0))
            s["entered_at"] = s["entered_at"] or leg.get("entry_time")
            s["stopped_at"] = s["stopped_at"] or leg.get("stop_time")
        return dict(s)


def _option_advice(leg, s, ltp, index_ok, phase):
    entry, targets = s["entry"], s["targets"]
    t1 = targets[0]
    near_entry = entry + NEAR_ENTRY_FRACTION * (t1 - entry)
    trail = _trail_stop(s)
    trend = "" if index_ok else " Note: the index is moving against this side."
    if s["stopped_at"]:
        return "EXIT", f"Stop loss hit at {s['stopped_at']}. No re-entry on this leg today."
    if s["targets_hit"] == len(targets):
        return "BOOK", "Every target is reached: book the profit."
    if phase == "closed":
        return "WAIT", "Market is closed."
    if not ltp:
        return "WAIT", "No live price yet."
    if not s["entered_at"]:
        if ltp >= entry:  # crossed between ticks, before tracking saw it
            return ("SKIP", f"Already at {ltp}, past target 1 {t1} without a tracked entry; don't chase.") \
                if ltp >= t1 else ("BUY", f"Entry {entry} crossed. Buy, stop {s['stop_loss']}.{trend}")
        return "WAIT", f"Buy only above {entry} ({_pct(entry, ltp)}% away). Stop {s['stop_loss']}, target 1 {t1}.{trend}"
    if s["targets_hit"] == 0:
        if ltp <= near_entry and index_ok:
            return "BUY", f"Entered at {s['entered_at']}; still near the entry (up to {round(near_entry, 2)}). " \
                          f"Buy, stop {s['stop_loss']}, target 1 {t1}."
        return "HOLD", f"In the trade since {s['entered_at']}. Too far above the entry to buy fresh; " \
                       f"stop {s['stop_loss']}, target 1 {t1}.{trend}"
    nxt = targets[s["targets_hit"]]
    if s["targets_hit"] == 1 and entry < ltp <= t1 and index_ok:
        return "ADD", f"Target 1 hit and the price has pulled back to {ltp} while staying above the entry {entry}. " \
                      f"Add; move the stop for the whole position to {entry}. Next target {nxt}."
    return "HOLD", f"{s['targets_hit']} of {len(targets)} targets hit. Trail the stop to {trail}; next target {nxt}." \
                   + ("" if s["targets_hit"] > 1 else f" Add on a pullback to {entry}-{t1}.") + trend


def _option_decision(leg, s, ltp, index_ok, phase, now):
    """_option_advice plus position size, the last entry time and the square-off time."""
    action, advice = _option_advice(leg, s, ltp, index_ok, phase)
    in_trade = s["entered_at"] and action in ("BUY", "ADD", "HOLD")
    if in_trade and now.time() >= risk.SQUARE_OFF:
        return "EXIT", f"{risk.SQUARE_OFF:%H:%M}: square off whatever is still open; nothing is carried past the close.", None
    if action not in ("BUY", "ADD"):
        return action, advice, None
    # An add risks its own price down to the entry, where the stop for the whole position moves.
    position = risk.size(ltp, s["stop_loss"] if action == "BUY" else s["entry"], leg["lotsize"])
    ok, detail = risk.fits_detail(position, "lot")
    if not ok:
        return "SKIP", f"{detail}.", position
    if now.time() >= risk.LAST_ENTRY:
        return ("HOLD" if s["entered_at"] else "WAIT"),             f"No new entries after {risk.LAST_ENTRY:%H:%M}: too little time left before the {risk.SQUARE_OFF:%H:%M} square-off.", None
    return action, f"{advice} Size: {detail}.", position


_seeded = set()  # (day, pair) already filled from candles
_seed_retry_at = {}  # pair -> epoch before which a failed fill is not retried (SmartAPI rate limit)
SEED_RETRY_SECONDS = 30


def _seed_from_candles(token, day):
    """Fill a leg's chart from today's 1-minute candles, once, so it starts at 09:15."""
    pair = ("NFO", str(token))
    if (day, pair) in _seeded or time.time() < _seed_retry_at.get(pair, 0):
        return
    _seed_retry_at[pair] = time.time() + SEED_RETRY_SECONDS
    now = timezone.localtime().strftime("%H:%M")
    try:
        response = smartapi.get_client().getCandleData({
            "exchange": "NFO", "symboltoken": str(token), "interval": "ONE_MINUTE",
            "fromdate": f"{day} 09:15", "todate": f"{day} {min(now, '15:30')}"})
        time.sleep(0.4)  # SmartAPI rate limit
    except Exception as exc:
        logger.warning("1-minute candles for %s failed: %s", token, exc)
        return
    rows = (response or {}).get("data") or []
    if not rows:  # rate limited or no trades yet: try again on the next refresh
        return
    _seeded.add((day, pair))
    live_feed.seed_history(pair, [(datetime.fromisoformat(r[0]).timestamp(), r[4]) for r in rows])


def _epoch(day, t):
    return timezone.make_aware(datetime.combine(day, t)).timestamp()


def _option_checks(s, ltp, index_ok, leg_type, index_ltp, ref):
    """The conditions behind the decision, so the screen can show why it is BUY / ADD or not."""
    entry, t1 = s["entry"], s["targets"][0]
    near_entry = round(entry + NEAR_ENTRY_FRACTION * (t1 - entry), 2)
    side = "above" if leg_type == "CE" else "below"
    checks = [
        {"name": "Stop not hit", "ok": not s["stopped_at"],
         "detail": f"Stop loss hit at {s['stopped_at']}" if s["stopped_at"] else f"Stop {_trail_stop(s)} not touched"},
        {"name": "Entry crossed", "ok": bool(s["entered_at"]) or bool(ltp and ltp >= entry),
         "detail": f"Crossed {entry} at {s['entered_at']}" if s["entered_at"] else f"Price {ltp} vs entry {entry}"},
        {"name": "Index agrees", "ok": index_ok,
         "detail": f"Index {index_ltp} vs reference close {ref}: a {leg_type} wants it {side}"},
    ]
    if s["targets_hit"] == 0:
        checks.append({"name": "Not chasing", "ok": bool(ltp and ltp <= near_entry),
                       "detail": f"Price {ltp} vs buy-up-to {near_entry} (close to the entry)"})
    else:
        checks.append({"name": "Pullback to add zone", "ok": bool(ltp and s["targets_hit"] == 1 and entry < ltp <= t1),
                       "detail": f"Add only after target 1, on a dip to {entry}-{t1}; price {ltp}"})
    return checks


def option_suggestions(underlying="NIFTY"):
    now = timezone.localtime()
    phase = _market_phase(now)
    day = timezone.localdate()
    if phase == "closed" and now.time() < MARKET_OPEN:  # no candles for today yet
        return {"underlying": underlying, "time": now.strftime("%H:%M:%S"), "market": phase, "index": {"ltp": None},
                "square": {"available_from": "09:30", "legs": []},
                "bounce_back": {"available_from": "10:15", "legs": []},
                "feed": live_feed.status(), "disclaimer": DISCLAIMER}
    levels = option_levels.option_levels(underlying, day)
    index_token = option_levels.INDEX_TOKENS[underlying]

    rules = []
    pairs = {("NSE", index_token)}
    for rule, ref_key in (("square", "first_15m_close"), ("bounce_back", "first_hour_close")):
        data = levels[rule]
        legs = [l for l in data.get("legs", []) if not l.get("error")]
        pairs.update(("NFO", l["token"]) for l in legs)
        rules.append((rule, ref_key, data, legs))
    quotes = live_feed.quotes(pairs)
    index_ltp = quotes.get(("NSE", index_token), {}).get("ltp") or levels["index"]["last"]

    result = {"underlying": underlying, "time": now.strftime("%H:%M:%S"), "market": phase,
              "index": {"ltp": index_ltp, **{k: v for k, v in levels["index"].items() if k != "last"}},
              "feed": live_feed.status(), "disclaimer": DISCLAIMER}
    for rule, ref_key, data, legs in rules:
        if "legs" not in data:
            result[rule] = {"available_from": data["available_from"], "legs": []}
            continue
        ref = levels["index"].get(ref_key)
        out = []
        for leg in data["legs"]:
            if leg.get("error"):
                out.append({"type": leg["type"], "strike": leg["strike"], "action": "WAIT", "advice": leg["error"]})
                continue
            s = _leg_state(day, rule, leg)
            ltp = quotes.get(("NFO", str(leg["token"])), {}).get("ltp")
            index_ok = ref is None or (index_ltp >= ref if leg["type"] == "CE" else index_ltp <= ref)
            action, advice, position = _option_decision(leg, s, ltp, index_ok, phase, now)
            checks = _option_checks(s, ltp, index_ok, leg["type"], index_ltp, ref)
            checks.append({"name": "Entry window", "ok": now.time() < risk.LAST_ENTRY,
                           "detail": f"New entries until {risk.LAST_ENTRY:%H:%M}, square off at {risk.SQUARE_OFF:%H:%M}"})
            if position is not None:
                checks.append({"name": "Fits your risk", "ok": position["lots"] > 0,
                               "detail": risk.fits_detail(position, "lot")[1]})
            _seed_from_candles(leg["token"], day)
            out.append({
                "symbol": leg["symbol"], "type": leg["type"], "strike": leg["strike"], "expiry": leg["expiry"],
                "lotsize": leg["lotsize"], "ltp": ltp, "entry": s["entry"], "stop_loss": s["stop_loss"],
                "trail_stop": _trail_stop(s) if s["entered_at"] else None, "targets": s["targets"],
                "entered_at": s["entered_at"], "targets_hit": s["targets_hit"], "stopped_at": s["stopped_at"],
                "index_agrees": index_ok, "action": action, "advice": advice, "position": position,
                "checks": checks,
                "history": [[round(t), p] for t, p in live_feed.history(("NFO", str(leg["token"])),
                                                                      since=_epoch(day, MARKET_OPEN))],
            })
        result[rule] = {"base_strike": data["base_strike"], "index_reference": ref, "legs": out}
    return result
