"""Time machine: replay a trade's next `hold` sessions thousands of times before risking money.

Two checks for one stock, entry, target and stop loss:

* simulation: each path draws `hold` daily candles at random (with replacement) from the stock's
  own last `lookback` candles, each candle as a move from the previous close (open, high, low and
  close ratios), and chains them from the entry. Fixed `seed`, so anyone can repeat a run.
* history: the same distances (target +x%, stop -y%) applied from the close of every past day in
  that window, judged on the candles that really followed.

Both use the backtest's fill rules (stocks.analysis.backtest): the stop is checked first, a candle
touching both levels counts as the stop, a gap through a level fills at the open, and a trade still
open after `hold` sessions exits at that close. No brokerage or taxes.

Daily candles are drawn independently, so calm and wild days get mixed and news days (results,
a block deal) only appear as often as they did in the window. Treat the odds as a reality check,
not a prediction. The history check is every day, not only days the setup fired.
"""
from datetime import date

import numpy as np
from django.db import connection

from stocks.analysis.backtest import _simulate
from stocks.models import HistoricStock

PATHS = 10_000
LOOKBACK = 250   # ~1 year of sessions to draw candles from
HOLD = 20        # the backtest's holding period
SEED = 19
COIN_FLIP_POINTS = 10  # target-first and stop-first within this many points = "close to a coin flip"


class NotEnoughCandles(ValueError):
    pass


def _candles(token, exchange, lookback, as_of=None):
    """The stock's last `lookback` + 1 candles on or before `as_of` as (dates, [[open, high, low, close]]), oldest first."""
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT date, open, high, low, close FROM (
                SELECT DISTINCT ON (date) date, open, high, low, close
                FROM {HistoricStock._meta.db_table}
                WHERE symboltoken = %s AND exchange = %s AND close > 0 AND date <= %s
                ORDER BY date DESC
            ) t ORDER BY date DESC LIMIT %s
            """,
            [str(token), exchange, as_of or date.max, lookback + 1],
        )
        rows = cursor.fetchall()[::-1]
    return np.array([r[0] for r in rows], dtype="datetime64[D]"), np.array([r[1:] for r in rows], dtype=float)


def _paths(px, entry, target, stop_loss, hold, paths, seed):
    """Outcome of every simulated path: (return, exit kind, sessions held) as arrays."""
    prev = px[:-1, 3:4]
    moves = px[1:] / prev  # open, high, low, close as ratios of the previous close
    moves[:, 1] = np.maximum(moves[:, 1], moves[:, [0, 3]].max(axis=1))  # keep bad prints consistent
    moves[:, 2] = np.minimum(moves[:, 2], moves[:, [0, 3]].min(axis=1))

    draw = moves[np.random.default_rng(seed).integers(0, len(moves), size=(paths, hold))]
    closes = entry * np.cumprod(draw[:, :, 3], axis=1)
    prev_close = np.concatenate([np.full((paths, 1), entry), closes[:, :-1]], axis=1)
    o, h, l = (prev_close * draw[:, :, k] for k in range(3))

    never = hold
    stop_at = np.where((l <= stop_loss).any(axis=1), (l <= stop_loss).argmax(axis=1), never)
    target_at = np.where((h >= target).any(axis=1), (h >= target).argmax(axis=1), never)
    stopped = (stop_at < never) & (stop_at <= target_at)
    hit = (target_at < never) & ~stopped

    rows = np.arange(paths)
    ret = closes[:, -1] / entry - 1
    ret = np.where(stopped, np.minimum(o[rows, np.minimum(stop_at, hold - 1)], stop_loss) / entry - 1, ret)
    ret = np.where(hit, np.maximum(o[rows, np.minimum(target_at, hold - 1)], target) / entry - 1, ret)
    kind = np.where(stopped, "stop", np.where(hit, "target", "time"))
    held = np.where(stopped, stop_at + 1, np.where(hit, target_at + 1, hold))
    return ret, kind, held


def _history(dates, px, target_pct, stop_pct, hold):
    """The same distances replayed from every past close that has `hold` later candles."""
    out = []
    for i in range(len(dates) - hold):
        close = px[i, 3]
        res = _simulate(dates, px, dates[i], close, close * (1 + target_pct), close * (1 + stop_pct), hold)
        if res is not None:
            out.append(res)
    return out


def _stats(ret, kind, held):
    n = len(ret)
    if not n:
        return None
    ret = np.asarray(ret) * 100
    kind, held = np.asarray(kind), np.asarray(held)
    to_target = held[kind == "target"]
    return {
        "trades": int(n),
        "target_first_pct": round(float((kind == "target").mean() * 100), 1),
        "stop_first_pct": round(float((kind == "stop").mean() * 100), 1),
        "still_open_pct": round(float((kind == "time").mean() * 100), 1),
        "avg_return_pct": round(float(ret.mean()), 2),
        "median_return_pct": round(float(np.median(ret)), 2),
        "worst_5pct_return_pct": round(float(np.percentile(ret, 5)), 2),
        "best_5pct_return_pct": round(float(np.percentile(ret, 95)), 2),
        "avg_sessions_to_target": round(float(to_target.mean()), 1) if len(to_target) else None,
    }


def _verdict(sim):
    edge = sim["target_first_pct"] - sim["stop_first_pct"]
    if abs(edge) < COIN_FLIP_POINTS:
        call = "close to a coin flip"
    elif edge > 0:
        call = "the target came first more often than the stop"
    else:
        call = "the stop came first more often than the target"
    expect = "positive" if sim["avg_return_pct"] > 0 else "negative"
    return (f"Target first in {sim['target_first_pct']}% of replays, stop first in {sim['stop_first_pct']}%: "
            f"{call}. Average result {sim['avg_return_pct']:+.2f}% per trade ({expect} before costs).")


def simulate(*, token, exchange, entry, target, stop_loss, hold=HOLD, paths=PATHS, lookback=LOOKBACK, seed=SEED,
             as_of=None):
    """Simulated and historical odds of target first / stop first / still open for one trade plan,
    from the candles on or before `as_of` (default: all), so a past-date check never sees the future."""
    if not stop_loss < entry < target:
        raise ValueError(f"Need stop loss < entry < target, got {stop_loss} / {entry} / {target}")
    dates, px = _candles(token, exchange, lookback, as_of)
    if len(px) < max(60, hold + 2):
        raise NotEnoughCandles(f"Only {len(px)} candles stored for token {token}; need at least {max(60, hold + 2)}")

    sim = _stats(*_paths(px, entry, target, stop_loss, hold, paths, seed))
    target_pct, stop_pct = target / entry - 1, stop_loss / entry - 1
    past = _history(dates, px, target_pct, stop_pct, hold)
    history = _stats(*zip(*past)) if past else None
    return {
        "entry": round(entry, 2), "target": round(target, 2), "stop_loss": round(stop_loss, 2),
        "target_pct": round(target_pct * 100, 2), "stop_loss_pct": round(stop_pct * 100, 2),
        "rules": {"hold_sessions": hold, "paths": paths, "seed": seed,
                  "drawn_from": f"{len(px) - 1} daily candles, {str(dates[0])} to {str(dates[-1])}"},
        "simulation": sim,
        "history": history,
        "verdict": _verdict(sim),
        "caveat": "A simulation from the stock's own last year, not a prediction: results, news and gaps "
                  "can do what that year never did. Costs are not included.",
    }
