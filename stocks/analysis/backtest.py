"""Walk-forward backtest of the buy-signal techniques on the stored candles.

Every `step` trading days: build the indicator snapshot from candles up to that date only, run
every technique, and simulate a trade for every stock in the liquid universe: buy at that day's
close, sell at the target (close + 3 ATR) or stop loss (close - 2 ATR), whichever is touched
first, otherwise at the close after `hold` sessions. A gap through a level fills at the open, and
a candle touching both levels counts as the stop. No brokerage or taxes are deducted.

Comparing each technique's trades with the trades of the whole universe on the same dates
shows whether the technique adds anything over buying at random.
"""
import io
import json
import logging
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from django.conf import settings
from django.db import connection

from stocks.analysis import signals as sg
from stocks.models import HistoricStock

logger = logging.getLogger(__name__)

STEP = 5
HOLD = 20
MIN_PRICE = 20.0
MIN_AVG_TURNOVER = 5_000_000


def results_path():
    return settings.DATA_DIR / "backtest.json"


def load_results():
    """The last saved backtest summary, or None."""
    try:
        return json.loads(results_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_all_candles():
    buf = io.StringIO()
    with connection.cursor() as cursor:
        cursor.copy_expert(f"""
            COPY (SELECT symbol, exchange, symboltoken, date, open, high, low, close, volume
                  FROM {HistoricStock._meta.db_table}
                  WHERE symbol IS NOT NULL AND close > 0 ORDER BY symbol, date)
            TO STDOUT WITH (FORMAT csv, HEADER)""", buf)
    buf.seek(0)
    df = pd.read_csv(buf, parse_dates=["date"],
                     dtype={"symbol": "category", "exchange": "category", "symboltoken": "category"})
    return df.drop_duplicates(["symbol", "date"], keep="last").reset_index(drop=True)


def _simulate(dates, px, day, entry, target, stop, hold):
    i = np.searchsorted(dates, np.datetime64(day), side="right")  # first bar after the signal day
    window = px[i:i + hold]
    if len(window) < hold:
        return None  # not enough later candles to judge
    for k, (o, h, l, c) in enumerate(window, start=1):
        if l <= stop:
            return min(o, stop) / entry - 1, "stop", k
        if h >= target:
            return max(o, target) / entry - 1, "target", k
    return window[-1][3] / entry - 1, "time", hold


def run(step=STEP, hold=HOLD, progress=None, start_date=None, end_date=None):
    """Simulated trades as a DataFrame: one row per (date, stock) with a flag per technique.

    `start_date` / `end_date` (dates, optional) limit the signal dates; candles after
    `end_date` are still used to judge how those trades ended.
    """
    candles = _load_all_candles()
    future = {sym: (g["date"].to_numpy(), g[["open", "high", "low", "close"]].to_numpy())
              for sym, g in candles.groupby("symbol", observed=True)}
    days = np.sort(candles["date"].unique())
    if start_date:
        days = days[days >= np.datetime64(start_date)]
    if end_date:
        days = days[days <= np.datetime64(end_date)]
    days = days[::step]

    trades = []
    for n, day in enumerate(days, start=1):
        window = candles[(candles["date"] <= day) & (candles["date"] >= day - pd.Timedelta(days=sg.LOOKBACK_DAYS))]
        snap = sg._build_snapshot(window.reset_index(drop=True))
        as_of = snap["date"].max()
        s = snap[(snap["date"] >= as_of - pd.Timedelta(days=sg.STALE_DAYS)) & (snap["bars"] >= sg.MIN_BARS)
                 & (snap["close"] >= MIN_PRICE) & (snap["avg_turnover"] >= MIN_AVG_TURNOVER)
                 & (snap["atr14"] > 0)].copy()
        if not s.empty:
            s["rs_score"] = s[["ret21", "ret63", "ret126"]].rank(pct=True).mean(axis=1, skipna=False) * 100
            fired = pd.DataFrame({name: sg._matches(t, s) for name, t in sg.TECHNIQUES.items()}, index=s.index)
            fired["top_picks"] = fired.sum(axis=1) >= 2
            for idx, row in s.iterrows():
                dates, px = future[row["symbol"]]
                res = _simulate(dates, px, row["date"], row["close"], row["target"], row["stop_loss"], hold)
                if res is not None:
                    trades.append({"as_of": as_of, "symbol": row["symbol"], "ret": res[0], "exit": res[1],
                                   "bars": res[2], **fired.loc[idx].to_dict()})
        if progress:
            progress(n, len(days), pd.Timestamp(as_of).date(), len(trades))
    return pd.DataFrame(trades)


def summarize(trades, step=STEP, hold=HOLD):
    """Per-technique track record, compared with every liquid stock on the same dates."""
    if trades.empty:
        raise ValueError("The backtest produced no trades: pick a wider date range "
                         f"(each trade needs {hold} later sessions to finish)")
    t = trades.copy()
    t["ret_pct"] = t["ret"] * 100
    t["month"] = pd.to_datetime(t["as_of"]).dt.to_period("M")
    base_by_date = t.groupby("as_of")["ret_pct"].mean()
    base_by_month = t.groupby("month")["ret_pct"].mean()

    def stats(df):
        r = df["ret_pct"]
        gains, losses = r[r > 0].sum(), -r[r < 0].sum()
        monthly = df.groupby("month")["ret_pct"].mean()
        beat = monthly > base_by_month.reindex(monthly.index)
        to_target = df.loc[df["exit"] == "target", "bars"]
        return {
            "trades": int(len(df)),
            "signal_dates": int(df["as_of"].nunique()),
            "avg_return_pct": round(float(r.mean()), 2),
            "median_return_pct": round(float(r.median()), 2),
            "excess_return_pct": round(float((r - df["as_of"].map(base_by_date)).mean()), 2),
            "win_rate_pct": round(float((r > 0).mean() * 100), 1),
            "target_hit_pct": round(float((df["exit"] == "target").mean() * 100), 1),
            "stop_hit_pct": round(float((df["exit"] == "stop").mean() * 100), 1),
            "profit_factor": round(float(gains / losses), 2) if losses else None,
            "months_beating_market": int(beat.sum()),
            "months": int(len(monthly)),
            "avg_days_to_target": round(float(to_target.mean()), 1) if len(to_target) else None,
        }

    names = [c for c in t.columns if c in sg.TECHNIQUES or c == "top_picks"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {"from": str(pd.Timestamp(t["as_of"].min()).date()), "to": str(pd.Timestamp(t["as_of"].max()).date())},
        "rules": {"step_days": step, "hold_days": hold, "entry": "close on signal day",
                  "exit": "target (close + 3 ATR), stop (close - 2 ATR) or close after hold_days",
                  "costs": "not included (brokerage + taxes are roughly 0.2-0.3% per round trip)"},
        "market": stats(t),
        "techniques": {name: stats(t[t[name]]) for name in names if t[name].any()},
    }


def run_and_save(step=STEP, hold=HOLD, progress=None, start_date=None, end_date=None):
    started = time.monotonic()
    summary = summarize(run(step, hold, progress, start_date, end_date), step, hold)
    summary["runtime_seconds"] = round(time.monotonic() - started)
    path = results_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
