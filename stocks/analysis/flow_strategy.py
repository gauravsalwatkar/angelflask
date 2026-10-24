"""Money-flow breakout: buy liquid stocks that money is pouring into, on 5-minute candles.

Checked on every 5-minute candle that closes between FIRST_SIGNAL and LAST_SIGNAL, at most one
trade per stock per day:

* heavy money: the day's traded value so far is at least `min_rel` x the stock's average traded
  value by the same time of day over its previous PROFILE_DAYS sessions (time-of-day aware, so the
  busy first hour does not count as heavy by itself);
* buyers in control: the close is above the day's VWAP and at least `min_change_pct` above the
  previous day's close;
* breakout: the close is above the opening range (the high of the first 15 minutes).

Entry at the next candle's open. Stop loss at the VWAP of the signal candle, at least
`min_stop_pct` below the entry; target `target_r` x that risk; otherwise out at the close of the
15:10 candle (15:15). The stop is checked first, a candle touching both counts as the stop and a
gap through a level fills at the open, as in the daily backtest. `cost_pct` per round trip
(brokerage, STT, exchange charges) is deducted.

Benchmark: each trade is compared with buying every stored stock at the same candle's open and
holding to 15:15, so "excess" says whether the signal beat simply being in the market then.

Live, the same rules run on the money-flow quotes: today's value from the quote (volume x VWAP)
against the stored time-of-day profile, and the opening range from today's first three 5-minute
candles (fetched once per candidate). The live check uses the last price, not a candle close.

A backtest on ~60 sessions is a short sample; the split into halves shows whether a result held
up across time. Not investment advice.
"""
import json
import logging
import time
from datetime import datetime, time as dtime

import numpy as np
import pandas as pd
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from stocks.analysis import risk
from stocks.services import intraday, smartapi
from stocks.services.historic import RATE_LIMIT_MARKER

logger = logging.getLogger(__name__)

SESSION_OPEN, OPENING_RANGE_END = dtime(9, 15), dtime(9, 30)
FIRST_SIGNAL, LAST_SIGNAL = dtime(9, 45), dtime(13, 0)   # candle close times
EXIT_CANDLE = dtime(15, 10)                              # out at the close of this candle (15:15)
BAR = pd.Timedelta(minutes=5)
PROFILE_DAYS, PROFILE_MIN_DAYS = 20, 10
MIN_DAY_CANDLES = 60  # skip half days and gaps in the data

DEFAULTS = {"min_rel": 1.5, "min_change_pct": 0.5, "target_r": 2.0, "min_stop_pct": 0.3, "cost_pct": 0.06}

# A rule set counts as proven when it clears all of these.
MIN_TRADES = 100
MIN_PROFIT_FACTOR = 1.1


def results_path():
    return settings.DATA_DIR / "flow_backtest.json"


def load_results():
    try:
        return json.loads(results_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _slot(t):
    """5-minute slot of a time of day (09:15 -> 111)."""
    return (t.hour * 60 + t.minute) // 5


# ---------- features ----------

def _features(df):
    """Per-candle features of one stock: VWAP, previous close, opening range, relative value."""
    df = df[(df["time"].dt.time >= SESSION_OPEN) & (df["time"].dt.time < dtime(15, 30))].sort_values("time").copy()
    df["day"] = df["time"].dt.normalize()
    df["slot"] = (df["time"].dt.hour * 60 + df["time"].dt.minute) // 5
    value = (df["high"] + df["low"] + df["close"]) / 3 * df["volume"]
    df["cum_value"] = value.groupby(df["day"]).cumsum()
    cum_volume = df["volume"].groupby(df["day"]).cumsum()
    df["vwap"] = np.where(cum_volume > 0, df["cum_value"] / cum_volume.where(cum_volume > 0), df["close"])

    by_day = df.groupby("day")
    df["prev_close"] = df["day"].map(by_day["close"].last().shift(1))
    df["day_candles"] = df["day"].map(by_day.size())
    df["or_high"] = df["day"].map(df[df["time"].dt.time < OPENING_RANGE_END].groupby("day")["high"].max())

    # Average value traded by the end of each slot over the previous sessions; a slot without
    # trades keeps the value so far.
    pivot = df.pivot_table(index="day", columns="slot", values="cum_value", aggfunc="last").sort_index().ffill(axis=1)
    profile = pivot.rolling(PROFILE_DAYS, min_periods=PROFILE_MIN_DAYS).mean().shift(1)
    profile = profile.reset_index().melt(id_vars="day", var_name="slot", value_name="normal_value")
    df = df.merge(profile, on=["day", "slot"], how="left")
    df["rel"] = df["cum_value"] / df["normal_value"]
    return df.reset_index(drop=True)


def _signals(f, p):
    end = (f["time"] + BAR).dt.time
    cond = (
        (end >= FIRST_SIGNAL) & (end <= LAST_SIGNAL) & (f["day_candles"] >= MIN_DAY_CANDLES)
        & (f["close"] > f["vwap"]) & (f["close"] > f["or_high"])
        & ((f["close"] / f["prev_close"] - 1) * 100 >= p["min_change_pct"])
        & (f["rel"] >= p["min_rel"])
    )
    return f[cond.fillna(False)].groupby("day").head(1)


def _levels(entry, vwap, p):
    stop = min(vwap, entry * (1 - p["min_stop_pct"] / 100))
    return stop, entry + p["target_r"] * (entry - stop)


def _trade(f, i, p):
    """Simulate the signal on row i of one stock's features; None if there is no candle to enter on."""
    day = f.at[i, "day"]
    exit_time = day + pd.Timedelta(hours=EXIT_CANDLE.hour, minutes=EXIT_CANDLE.minute)
    bars = f.iloc[i + 1:]
    bars = bars[(bars["day"] == day) & (bars["time"] <= exit_time)]
    if bars.empty or bars["time"].iloc[-1] != exit_time:
        return None
    entry = bars["open"].iloc[0]
    stop, target = _levels(entry, f.at[i, "vwap"], p)
    exit_price, kind, exit_at = bars["close"].iloc[-1], "close", bars["time"].iloc[-1]
    for t, o, h, l in zip(bars["time"], bars["open"], bars["high"], bars["low"]):
        if l <= stop:
            exit_price, kind, exit_at = min(o, stop), "stop", t
            break
        if h >= target:
            exit_price, kind, exit_at = max(o, target), "target", t
            break
    gross = (exit_price / entry - 1) * 100
    return {
        "day": day, "entry_time": bars["time"].iloc[0], "entry": entry, "stop_loss": stop, "target": target,
        "exit": exit_price, "exit_kind": kind, "exit_time": exit_at + BAR,
        "gross_pct": gross, "net_pct": gross - p["cost_pct"],
        "rel": f.at[i, "rel"], "change_pct": (f.at[i, "close"] / f.at[i, "prev_close"] - 1) * 100,
    }


def _to_close(f):
    """Return from each candle's open to the 15:15 close, for the market benchmark."""
    exit_close = f[f["time"].dt.time == EXIT_CANDLE].set_index("day")["close"]
    out = f[f["time"].dt.time < EXIT_CANDLE][["day", "time", "open"]].copy()
    out["ret_pct"] = (out["day"].map(exit_close) / out["open"] - 1) * 100
    return out.dropna()


# ---------- backtest ----------

def run(params=None):
    """Simulated trades over every stored stock, as a DataFrame, plus the number of stocks."""
    p = {**DEFAULTS, **(params or {})}
    stocks = intraday.stored_stocks()
    trades, market = [], []
    for token, symbol in stocks.items():
        df = intraday.load(token)
        if df is None or df.empty:
            continue
        f = _features(df)
        market.append(_to_close(f))
        for i in _signals(f, p).index:
            t = _trade(f, i, p)
            if t:
                trades.append({"symbol": symbol, **t})
    if not trades:
        return pd.DataFrame(), len(stocks)
    t = pd.DataFrame(trades)
    bench = pd.concat(market).groupby("time")["ret_pct"].mean()
    t["market_pct"] = t["entry_time"].map(bench)
    t["excess_pct"] = t["gross_pct"] - t["market_pct"]
    return t.sort_values("entry_time").reset_index(drop=True), len(stocks)


def _stats(t):
    if t.empty:
        return None
    r = t["net_pct"]
    gains, losses = r[r > 0].sum(), -r[r < 0].sum()
    return {
        "trades": int(len(t)),
        "days": int(t["day"].nunique()),
        "avg_net_pct": round(float(r.mean()), 3),
        "median_net_pct": round(float(r.median()), 3),
        "win_rate_pct": round(float((r > 0).mean() * 100), 1),
        "profit_factor": round(float(gains / losses), 2) if losses else None,
        "target_pct": round(float((t["exit_kind"] == "target").mean() * 100), 1),
        "stop_pct": round(float((t["exit_kind"] == "stop").mean() * 100), 1),
        "close_pct": round(float((t["exit_kind"] == "close").mean() * 100), 1),
        "avg_excess_pct": round(float(t["excess_pct"].mean()), 3),
        "total_net_pct": round(float(r.sum()), 2),
    }


def is_proven(s):
    return bool(s and s["trades"] >= MIN_TRADES and s["avg_net_pct"] > 0 and s["avg_excess_pct"] > 0
                and (s["profit_factor"] or 0) >= MIN_PROFIT_FACTOR
                and all(h and h["avg_net_pct"] > 0 for h in s.get("halves", [])))


def summarize(t, stocks, p):
    days = np.sort(t["day"].unique())
    mid = days[len(days) // 2]
    overall = _stats(t)
    overall["halves"] = [_stats(t[t["day"] < mid]), _stats(t[t["day"] >= mid])]
    entry_end = (t["entry_time"].dt.hour * 60 + t["entry_time"].dt.minute)
    by_time = {label: _stats(t[(entry_end >= lo) & (entry_end < hi)]) for label, lo, hi in
               (("09:45-10:30", 585, 630), ("10:30-11:30", 630, 690), ("11:30-13:00", 690, 785))}
    by_rel = {label: _stats(t[(t["rel"] >= lo) & (t["rel"] < hi)]) for label, lo, hi in
              ((f"{p['min_rel']}-2x", p["min_rel"], 2), ("2-3x", 2, 3), ("3x+", 3, np.inf)) if lo < hi}
    daily = t.groupby("day")["net_pct"].mean()
    last = t.tail(50).iloc[::-1]
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "params": p,
        "period": {"from": str(pd.Timestamp(days[0]).date()), "to": str(pd.Timestamp(days[-1]).date()),
                   "sessions": int(len(days)), "stocks": stocks},
        "overall": overall,
        "proven": is_proven(overall),
        "criteria": f"At least {MIN_TRADES} trades, average net and excess return above 0, profit factor "
                    f">= {MIN_PROFIT_FACTOR}, and positive in both halves of the period.",
        "by_entry_time": by_time,
        "by_relative_value": by_rel,
        "trades_per_day": round(float(t.groupby("day").size().mean()), 1),
        "best_day_pct": round(float(daily.max()), 2), "worst_day_pct": round(float(daily.min()), 2),
        "recent_trades": [{
            "symbol": r.symbol, "day": str(pd.Timestamp(r.day).date()),
            "entry_time": r.entry_time.strftime("%H:%M"), "entry": round(r.entry, 2),
            "stop_loss": round(r.stop_loss, 2), "target": round(r.target, 2),
            "exit": round(r.exit, 2), "exit_kind": r.exit_kind, "exit_time": r.exit_time.strftime("%H:%M"),
            "net_pct": round(r.net_pct, 2), "rel": round(r.rel, 1),
        } for r in last.itertuples()],
        "disclaimer": "Simulated on past 5-minute candles; a short sample, before slippage. Not investment advice.",
    }


def run_and_save(params=None):
    started = time.monotonic()
    p = {**DEFAULTS, **(params or {})}
    t, stocks = run(p)
    if t.empty:
        raise ValueError("No trades: download the 5-minute candles first, or loosen the rules")
    summary = summarize(t, stocks, p)
    summary["runtime_seconds"] = round(time.monotonic() - started, 1)
    path = results_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


# ---------- live ----------

def _profile(token, today):
    """Average value traded by the end of each slot over the last PROFILE_DAYS stored sessions before today."""
    key = f"flow-profile:{token}:{today}"
    profile = cache.get(key)
    if profile is None:
        df = intraday.load(token)
        profile = {}
        if df is not None:
            f = _features(df[df["time"].dt.date < today])
            full = f[f["day_candles"] >= MIN_DAY_CANDLES]
            pivot = full.pivot_table(index="day", columns="slot", values="cum_value", aggfunc="last").ffill(axis=1)
            if len(pivot) >= PROFILE_MIN_DAYS:
                profile = pivot.tail(PROFILE_DAYS).mean().dropna().to_dict()
        cache.set(key, profile, 12 * 60 * 60)
    return profile


def _opening_high(token, day):
    """High of today's first 15 minutes from three 5-minute candles; cached for the day once complete."""
    key = f"flow-or-high:{token}:{day}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        response = smartapi.get_client().getCandleData({
            "exchange": "NSE", "symboltoken": token, "interval": "FIVE_MINUTE",
            "fromdate": f"{day} 09:15", "todate": f"{day} 09:30"})
        time.sleep(0.4)  # SmartAPI rate limit
    except Exception as exc:
        logger.warning("Opening range for %s failed: %s", token, exc)
        return None
    if RATE_LIMIT_MARKER in str((response or {}).get("message", "")).lower():
        return None
    rows = [r for r in (response or {}).get("data") or [] if r[0][11:16] < "09:30"]
    if len(rows) < 3:
        return None
    high = max(r[2] for r in rows)
    cache.set(key, high, 12 * 60 * 60)
    return high


def _check(name, ok, detail):
    return {"name": name, "ok": bool(ok), "detail": detail}


def live(flow):
    """Today's signals on the money-flow quotes (an equity_flow result), with the last backtest's rules."""
    bt = load_results()
    p = {**DEFAULTS, **(bt["params"] if bt else {})}
    now = timezone.localtime()
    today = now.date()
    slot = _slot(now.time()) if flow["market"] == "open" else _slot(dtime(15, 25))
    in_window = flow["market"] == "open" and FIRST_SIGNAL <= now.time() <= LAST_SIGNAL
    window = f"New signals {FIRST_SIGNAL:%H:%M}-{LAST_SIGNAL:%H:%M}; out by 15:15"

    rows = []
    for r in flow["buying"]:
        if r["exchange"] != "NSE" or (r["change_pct"] or 0) < p["min_change_pct"]:
            continue
        profile = _profile(r["token"], today)
        normal = profile.get(slot)
        rel = round(r["value_cr"] * 1e7 / normal, 2) if normal else None
        if rel is None or rel < p["min_rel"]:
            continue
        or_high = _opening_high(r["token"], today) if now.time() >= OPENING_RANGE_END else None
        ltp, vwap = r["ltp"], r["vwap"]
        stop, target = _levels(ltp, vwap, p)
        position = risk.size(ltp, stop)
        checks = [
            _check("Entry window", in_window, window),
            _check("Heavy money", True, f"{rel}x its usual value by this time (rule: {p['min_rel']}x)"),
            _check("Up on the day", True, f"{r['change_pct']:+.2f}% (rule: +{p['min_change_pct']}%)"),
            _check("Above VWAP", ltp > vwap, f"Price {ltp} vs VWAP {vwap}"),
            _check("Opening range breakout", or_high is not None and ltp > or_high,
                   f"Price {ltp} vs first-15-minute high {or_high}" if or_high else "Opening range not available yet"),
            _check("Fits your risk", *risk.fits_detail(position)),
        ]
        rows.append({
            "symbol": r["symbol"], "token": r["token"], "ltp": ltp, "vwap": vwap, "change_pct": r["change_pct"],
            "relative_value": rel, "opening_high": or_high,
            "action": "SIGNAL" if all(c["ok"] for c in checks) else "WATCH",
            "entry": ltp, "stop_loss": round(stop, 2), "target": round(target, 2), "position": position,
            "checks": checks,
        })
    rows.sort(key=lambda x: (x["action"] != "SIGNAL", -x["relative_value"]))
    return {
        "params": p, "window": window, "signals": rows,
        "missing_profiles": not intraday.stored_stocks(),
        "backtest": {k: bt[k] for k in ("period", "overall", "proven", "params", "generated_at")} if bt else None,
        "note": "Rules on live prices, not investment advice. No orders are placed.",
    }
