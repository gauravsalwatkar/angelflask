"""Paper trading: three virtual books (intraday, short-term, long-term), PAPER_CAPITAL rupees each.

* Orders fill at the live price (stocks.services.live_feed) as market orders; no real order is
  ever placed. Without a quantity, a trade with a stop loss is sized by stocks.analysis.risk.
* Intraday: buy or sell short, only between 09:15 and the square-off (risk.SQUARE_OFF); anything
  still open is squared off at 15:15, at the close of the 15:10 five-minute candle when the
  check runs later. Short-term and long-term: buy only, held until the stop, the target or a
  manual close.
* Stops and targets are checked whenever the book is viewed, against every price the live feed
  recorded since the entry (about one point per 10 s, while the server runs and the stock is
  watched) and, for the multi-day books, the stored daily candles after the entry day. A stop
  fills at the price that crossed it (so a gap costs more), a target at the target; when a daily
  candle touches both, the stop counts.
* Round-trip costs (COST_PCT of the traded value, split over entry and exit) are deducted at the
  exit. Cash is not leveraged: a position, long or short, ties up its full value.
* Every trade keeps what it was based on (technique, signal, plan date), when its target was first
  reached, and the best and worst price seen while open, so `stats` can say which techniques work
  and how long their targets take. Trades are closed with a conditional update, so the automatic
  trader (stocks.services.autotrade) and the dashboard can check the same book safely.

A simulation of fills, not of the real market: no slippage beyond the above, no partial fills.
"""
import logging
import time
from collections import defaultdict
from datetime import datetime, time as dtime

import numpy as np

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from stocks.analysis import outcomes, risk, signals
from stocks.models import PaperTrade
from stocks.services import live_feed, money_flow, smartapi

logger = logging.getLogger(__name__)

COST_PCT = {PaperTrade.INTRADAY: 0.06, PaperTrade.SHORT_TERM: 0.25, PaperTrade.LONG_TERM: 0.25}
MARKET_OPEN, MARKET_CLOSE = dtime(9, 15), dtime(15, 30)
SQUARE_OFF_CANDLE = dtime(15, 10)  # its close is the 15:15 square-off price
CLOSED_SHOWN = 100


class PaperError(ValueError):
    pass


def _round(v):
    return round(v, 2) if v is not None else None


# ---------- instruments and prices ----------

def resolve(symbol, book):
    """(symbol, exchange, token) for a typed symbol: NSE for intraday, else the stored daily-candle exchange."""
    sym = symbol.strip().upper().removesuffix("-EQ")
    nse, _ = money_flow._nse_equity_tokens()
    if book == PaperTrade.INTRADAY and sym in nse:
        return sym, "NSE", nse[sym]
    snap = signals.get_snapshot()
    row = snap[snap["symbol"].str.upper() == sym]
    if not row.empty:
        return row["symbol"].iloc[0], row["exchange"].iloc[0], str(row["token"].iloc[0])
    if sym in nse:
        return sym, "NSE", nse[sym]
    raise PaperError(f"Unknown symbol '{symbol}'")


def _ltp(exchange, token):
    q = live_feed.quotes({(exchange, str(token))}).get((exchange, str(token)), {})
    return q.get("ltp"), q


def _square_off_price(trade, day):
    """Close of the 15:10 five-minute candle on `day` (cached), or None."""
    key = f"paper-square-off:{trade.exchange}:{trade.token}:{day}"
    price = cache.get(key)
    if price is not None:
        return price
    try:
        response = smartapi.get_client().getCandleData({
            "exchange": trade.exchange, "symboltoken": trade.token, "interval": "FIVE_MINUTE",
            "fromdate": f"{day} {SQUARE_OFF_CANDLE:%H:%M}", "todate": f"{day} 15:15"})
        time.sleep(0.4)  # SmartAPI rate limit
    except Exception as exc:
        logger.warning("Square-off candle for %s failed: %s", trade.symbol, exc)
        return None
    rows = [r for r in (response or {}).get("data") or [] if r[0][11:16] == f"{SQUARE_OFF_CANDLE:%H:%M}"]
    if not rows:
        return None
    cache.set(key, rows[0][4], 24 * 60 * 60)
    return rows[0][4]


# ---------- exits ----------

def _walk(trade, path):
    """Walk `path` [(when, high, low)]: the first (price, reason, when) reaching the stop or the target
    (or None), and the best / worst price seen up to then."""
    d, stop, target = trade.direction, trade.stop_loss, trade.target
    better = max if d > 0 else min
    worse = min if d > 0 else max
    best = trade.best_price if trade.best_price is not None else trade.entry_price
    worst = trade.worst_price if trade.worst_price is not None else trade.entry_price
    for when, high, low in path:
        adverse, favourable = (low, high) if d > 0 else (high, low)
        if stop is not None and (adverse - stop) * d <= 0:
            # A live point is a traded price: fill there (worse than the stop on a gap). A daily
            # candle has no time order inside it, so its low/high only says the stop was reached.
            return ((adverse if high == low else stop), "stop", when), best, worse(worst, adverse)
        if target is not None and (favourable - target) * d >= 0:
            return (target, "target", when), better(best, favourable), worst
        best, worst = better(best, favourable), worse(worst, adverse)
    return None, best, worst


def _price_path(trade, now):
    """Prices since the entry, oldest first: daily candles after the entry day, then live points."""
    path = []
    entry_day = timezone.localtime(trade.entry_at).date()
    if trade.book != PaperTrade.INTRADAY:
        candles = outcomes._candles_after({(trade.token, trade.exchange)}, entry_day)
        for day, high, low, _close in candles.get((trade.token, trade.exchange), []):
            path.append((timezone.make_aware(datetime.combine(day, MARKET_CLOSE)), high, low))
    for ts, price in live_feed.history((trade.exchange, trade.token), since=trade.entry_at.timestamp()):
        path.append((datetime.fromtimestamp(ts, tz=timezone.get_current_timezone()), price, price))
    return sorted(path, key=lambda p: p[0])


def _close(trade, price, reason, when):
    """Close an open trade; False if another process closed it first."""
    costs = round((trade.entry_price + price) * trade.qty * COST_PCT[trade.book] / 200, 2)
    fields = {
        "status": PaperTrade.CLOSED, "exit_price": price, "exit_at": when, "exit_reason": reason, "costs": costs,
        "pnl": round((price - trade.entry_price) * trade.qty * trade.direction - costs, 2),
        "best_price": trade.best_price, "worst_price": trade.worst_price,
    }
    if reason == "target" and trade.target_hit_at is None:
        fields["target_hit_at"] = when
    if not PaperTrade.objects.filter(pk=trade.pk, status=PaperTrade.OPEN).update(**fields):
        return False
    for k, v in fields.items():
        setattr(trade, k, v)
    return True


def _check_exits(trades, quotes, now):
    for t in trades:
        hit, best, worst = _walk(t, _price_path(t, now))
        if (best, worst) != (t.best_price, t.worst_price):
            t.best_price, t.worst_price = best, worst
            PaperTrade.objects.filter(pk=t.pk, status=PaperTrade.OPEN).update(best_price=best, worst_price=worst)
        if hit:
            _close(t, *hit)
            continue
        if t.book != PaperTrade.INTRADAY:
            continue
        entry_day = timezone.localtime(t.entry_at).date()
        if entry_day == now.date() and now.time() < risk.SQUARE_OFF:
            continue
        ltp = quotes.get((t.exchange, t.token), {}).get("ltp")
        late = entry_day < now.date() or now.time() >= dtime(15, 20)
        price = (_square_off_price(t, entry_day) if late else None) or ltp
        if price:
            when = timezone.make_aware(datetime.combine(entry_day, risk.SQUARE_OFF))
            _close(t, price, "square_off", min(when, now))


# ---------- orders ----------

def place(*, book, symbol, side=PaperTrade.BUY, qty=None, stop_loss=None, target=None, source="manual", notes="",
          technique="manual", auto=False, signal=None, signal_date=None):
    """Open a paper trade at the live price, recording the technique and signal behind it."""
    now = timezone.localtime()
    if side == PaperTrade.SELL and book != PaperTrade.INTRADAY:
        raise PaperError("Short selling is only for the intraday book")
    if book == PaperTrade.INTRADAY and (now.weekday() >= 5 or not MARKET_OPEN <= now.time() < risk.SQUARE_OFF):
        raise PaperError(f"Intraday paper orders are taken {MARKET_OPEN:%H:%M}-{risk.SQUARE_OFF:%H:%M} on trading days")
    symbol, exchange, token = resolve(symbol, book)
    ltp, _ = _ltp(exchange, token)
    if not ltp:
        raise PaperError(f"No live price for {symbol} right now")

    d = 1 if side == PaperTrade.BUY else -1
    if stop_loss is not None and (ltp - stop_loss) * d <= 0:
        raise PaperError(f"The stop loss {stop_loss} must be {'below' if d > 0 else 'above'} the price {ltp}")
    if target is not None and (target - ltp) * d <= 0:
        raise PaperError(f"The target {target} must be {'above' if d > 0 else 'below'} the price {ltp}")
    if not qty:
        if stop_loss is None:
            raise PaperError("Give a quantity, or a stop loss to size the trade from your max loss")
        # risk.size works on a long; a short mirrors its stop below the price.
        position = risk.size(ltp, ltp - abs(ltp - stop_loss))
        qty = position["qty"]
        if not qty:
            raise PaperError(risk.fits_detail(position)[1])

    with transaction.atomic():
        cash = summary(book, {})["cash"]
        if ltp * qty > cash:
            raise PaperError(f"{qty} x {ltp} = ₹{ltp * qty:,.0f} is more than the book's cash ₹{cash:,.0f}")
        return PaperTrade.objects.create(
            book=book, symbol=symbol, exchange=exchange, token=token, side=side, qty=qty,
            entry_price=ltp, entry_at=now, stop_loss=stop_loss, target=target, source=source, notes=notes,
            technique=technique or "manual", auto=auto, signal=signal or {}, signal_date=signal_date,
            best_price=ltp, worst_price=ltp)


def close(trade_id):
    """Close an open paper trade at the live price."""
    trade = PaperTrade.objects.filter(pk=trade_id, status=PaperTrade.OPEN).first()
    if trade is None:
        raise PaperError("No open paper trade with that id")
    ltp, _ = _ltp(trade.exchange, trade.token)
    if not ltp:
        raise PaperError(f"No live price for {trade.symbol} right now")
    if not _close(trade, ltp, "manual", timezone.localtime()):
        raise PaperError("This trade was just closed by its stop, target or square-off")
    return trade


def reset(book):
    deleted, _ = PaperTrade.objects.filter(book=book).delete()
    return deleted


# ---------- the book ----------

def _duration(start, end):
    """(minutes, trading days, plain words) between two datetimes."""
    start, end = timezone.localtime(start), timezone.localtime(end)
    minutes = round((end - start).total_seconds() / 60)
    days = int(np.busday_count(start.date(), end.date()))  # weekdays; exchange holidays not excluded
    if days == 0:
        words = f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes}m"
    else:
        words = f"{days} trading day{'s' if days > 1 else ''}"
    return minutes, days, words


def _excursion_pct(t, price):
    return _round((price / t.entry_price - 1) * 100 * t.direction) if price is not None else None


def _row(t, ltp=None):
    price = t.exit_price if t.status == PaperTrade.CLOSED else ltp
    to_target = _duration(t.entry_at, t.target_hit_at) if t.target_hit_at else None
    held = _duration(t.entry_at, t.exit_at or timezone.localtime())
    gross = (price - t.entry_price) * t.qty * t.direction if price else None
    risk_rupees = abs(t.entry_price - t.stop_loss) * t.qty if t.stop_loss is not None else None
    pnl = t.pnl if t.status == PaperTrade.CLOSED else gross
    return {
        "id": t.id, "symbol": t.symbol, "exchange": t.exchange, "side": t.side, "qty": t.qty,
        "entry_price": t.entry_price, "entry_at": timezone.localtime(t.entry_at).strftime("%Y-%m-%d %H:%M"),
        "stop_loss": t.stop_loss, "target": t.target, "source": t.source, "notes": t.notes,
        "value": _round(t.entry_price * t.qty), "ltp": ltp, "status": t.status,
        "exit_price": t.exit_price, "exit_reason": t.exit_reason,
        "exit_at": timezone.localtime(t.exit_at).strftime("%Y-%m-%d %H:%M") if t.exit_at else None,
        "costs": t.costs, "pnl": _round(pnl),
        "pnl_pct": _round(pnl / (t.entry_price * t.qty) * 100) if pnl is not None else None,
        "r_multiple": _round(pnl / risk_rupees) if pnl is not None and risk_rupees else None,
        "technique": t.technique, "auto": t.auto, "signal": t.signal,
        "signal_date": t.signal_date.isoformat() if t.signal_date else None,
        "target_hit": t.target_hit_at is not None if t.target is not None else None,
        "target_hit_at": timezone.localtime(t.target_hit_at).strftime("%Y-%m-%d %H:%M") if t.target_hit_at else None,
        "time_to_target": to_target[2] if to_target else None,
        "minutes_to_target": to_target[0] if to_target else None,
        "held": held[2],
        "best_pct": _excursion_pct(t, t.best_price), "worst_pct": _excursion_pct(t, t.worst_price),
    }


def summary(book, quotes):
    """Cash, value and P&L of a book (open positions valued at `quotes`)."""
    capital = settings.PAPER_CAPITAL
    trades = list(PaperTrade.objects.filter(book=book))
    open_ = [t for t in trades if t.status == PaperTrade.OPEN]
    closed = [t for t in trades if t.status == PaperTrade.CLOSED]
    realized = sum(t.pnl for t in closed)
    invested = sum(t.entry_price * t.qty for t in open_)
    open_pnl = sum((quotes.get((t.exchange, t.token), {}).get("ltp") - t.entry_price) * t.qty * t.direction
                   for t in open_ if quotes.get((t.exchange, t.token), {}).get("ltp"))
    wins = [t.pnl for t in closed if t.pnl > 0]
    losses = [-t.pnl for t in closed if t.pnl <= 0]
    equity = capital + realized + open_pnl
    return {
        "capital": capital, "cash": _round(capital + realized - invested), "invested": _round(invested),
        "open_pnl": _round(open_pnl), "realized_pnl": _round(realized), "equity": _round(equity),
        "return_pct": _round((equity / capital - 1) * 100),
        "open_positions": len(open_), "closed_trades": len(closed),
        "win_rate_pct": _round(len(wins) / len(closed) * 100) if closed else None,
        "profit_factor": _round(sum(wins) / sum(losses)) if losses and sum(losses) else None,
        "avg_pnl": _round(realized / len(closed)) if closed else None,
        "costs_paid": _round(sum(t.costs for t in closed)),
    }


def trade_dates(name):
    """Days with trades bought in the book, newest first: [{date, trades, open, pnl}] (pnl = booked so far)."""
    days = defaultdict(lambda: {"trades": 0, "open": 0, "pnl": 0.0})
    for t in PaperTrade.objects.filter(book=name).only("entry_at", "status", "pnl"):
        d = days[timezone.localtime(t.entry_at).date()]
        d["trades"] += 1
        d["open"] += t.status == PaperTrade.OPEN
        d["pnl"] += t.pnl or 0
    return [{"date": day.isoformat(), **v, "pnl": _round(v["pnl"])} for day, v in sorted(days.items(), reverse=True)]


def book(name, day=None):
    """The book after running its exits: summary, open positions at live prices, recent closed trades.

    With `day`, the open and closed lists hold only the trades bought on that day (all of them), and
    `day_summary` scores them: target hits, time to target, win rate and P&L.
    """
    now = timezone.localtime()
    open_trades = list(PaperTrade.objects.filter(book=name, status=PaperTrade.OPEN))
    pairs = {(t.exchange, t.token) for t in open_trades}
    quotes = live_feed.quotes(pairs) if pairs else {}
    _check_exits(open_trades, quotes, now)

    open_trades = PaperTrade.objects.filter(book=name, status=PaperTrade.OPEN)
    closed = PaperTrade.objects.filter(book=name, status=PaperTrade.CLOSED).order_by("-exit_at", "-id")
    day_summary = None
    if day:
        open_trades, closed = open_trades.filter(entry_at__date=day), closed.filter(entry_at__date=day)
        day_summary = {"date": day.isoformat(), **_group_stats(list(open_trades) + list(closed))}
    else:
        closed = closed[:CLOSED_SHOWN]
    return {
        "book": name, "time": now.strftime("%H:%M:%S"), "date": day.isoformat() if day else None,
        "day_summary": day_summary, "dates": trade_dates(name),
        "market": "open" if now.weekday() < 5 and MARKET_OPEN <= now.time() < MARKET_CLOSE else "closed",
        "summary": summary(name, quotes),
        "open": [_row(t, quotes.get((t.exchange, t.token), {}).get("ltp")) for t in open_trades],
        "closed": [_row(t) for t in closed],
        "cost_pct": COST_PCT[name],
        "rules": ("Bought or shorted at the live price 09:15-15:15, squared off at 15:15." if name == PaperTrade.INTRADAY
                  else "Bought at the live price, held until the stop, the target or your close."),
        "note": "Paper trading: simulated fills at live prices. No real order is placed.",
    }


# ---------- what works ----------

def _group_stats(trades):
    closed = [t for t in trades if t.status == PaperTrade.CLOSED]
    with_target = [t for t in trades if t.target is not None]
    hits = [t for t in with_target if t.target_hit_at]
    pnl = [t.pnl for t in closed]
    risked = [(t.pnl / (abs(t.entry_price - t.stop_loss) * t.qty)) for t in closed
              if t.stop_loss is not None and t.entry_price != t.stop_loss]
    minutes = [_duration(t.entry_at, t.target_hit_at)[0] for t in hits]
    days = [_duration(t.entry_at, t.target_hit_at)[1] for t in hits]
    wins, losses = sum(p for p in pnl if p > 0), -sum(p for p in pnl if p < 0)
    mean = lambda xs: _round(sum(xs) / len(xs)) if xs else None
    return {
        "trades": len(trades), "open": len(trades) - len(closed), "closed": len(closed),
        "target_hit_pct": _round(len(hits) / len(with_target) * 100) if with_target else None,
        "stop_hit_pct": _round(sum(t.exit_reason == "stop" for t in closed) / len(closed) * 100) if closed else None,
        "win_rate_pct": _round(sum(p > 0 for p in pnl) / len(pnl) * 100) if pnl else None,
        "avg_pnl": mean(pnl), "total_pnl": _round(sum(pnl)),
        "profit_factor": _round(wins / losses) if losses else None,
        "avg_r": mean(risked),
        "avg_minutes_to_target": mean(minutes), "avg_days_to_target": mean(days),
        "avg_best_pct": mean([_excursion_pct(t, t.best_price) for t in trades if t.best_price is not None]),
        "avg_worst_pct": mean([_excursion_pct(t, t.worst_price) for t in trades if t.worst_price is not None]),
    }


def stats(book=None):
    """Track record per book and technique, and per single technique (a '+'-joined trade counts for each)."""
    qs = PaperTrade.objects.all() if book is None else PaperTrade.objects.filter(book=book)
    trades = list(qs)
    by_combo, by_single = defaultdict(list), defaultdict(list)
    for t in trades:
        by_combo[(t.book, t.technique)].append(t)
        for name in t.technique.split("+"):
            by_single[(t.book, name)].append(t)
    rows = lambda groups: sorted(
        ({"book": b, "technique": name, **_group_stats(ts)} for (b, name), ts in groups.items()),
        key=lambda r: (r["book"], -(r["total_pnl"] or 0)))
    return {
        "overall": {b: _group_stats([t for t in trades if t.book == b]) for b in {t.book for t in trades}},
        "by_technique": rows(by_single),
        "by_combination": rows(by_combo),
        "note": "Target hit % counts trades with a target that reached it while open. Best / worst = the "
                "most favourable / adverse move seen while open, which shows if stops are too tight or targets too far.",
    }
