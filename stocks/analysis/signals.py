"""Buy-signal techniques evaluated on the latest daily candle of every symbol.

Pass `as_of` (a date) to evaluate them on the last candle on or before that date instead,
using only candles up to it: a prediction "made" that day, which can then be verified
against the candles that followed.

Indicators are computed in one vectorised pass over all candles (sorted by symbol,
date). Only the last row per symbol is kept as a "snapshot", which is cached until
new candles are imported. Each technique is a filter + ranking over that snapshot,
and `top_picks` combines them into a weighted score.

These are mechanical technical-analysis screens, not investment advice.
"""
import logging
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd
from django.core.cache import cache
from django.db import connection

from stocks.models import HistoricStock

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 420      # calendar days loaded (~285 sessions): enough for SMA200 and a 52-week high
BARS_52W = 252
MIN_BARS = 60            # symbols with less history are skipped
STALE_DAYS = 5           # symbols whose last candle is older than the latest data by more than this are skipped
CACHE_TIMEOUT = 6 * 60 * 60
SWING_ORDER = 5          # a swing low is the strict lowest low of the 5 bars on each side
SUPPORT_ATR = 0.5        # a swing low within this many ATRs of the pattern low counts as a support touch
MIN_TOUCHES = 2          # earlier swing lows needed to call a level support

_compute_lock = threading.Lock()


# --------------------------------------------------------------------------- loading

def _load_candles(as_of=None):
    """Load candles up to `as_of` (default: the latest) for all symbols, sorted by (symbol, date).

    Uses COPY for speed. `as_of` is a datetime.date, so inlining its ISO form is safe.
    """
    table = HistoricStock._meta.db_table
    end = f"DATE '{as_of.isoformat()}'" if as_of else f"(SELECT max(date) FROM {table})"
    sql = f"""
        COPY (
            SELECT symbol, exchange, symboltoken, date, open, high, low, close, volume
            FROM {table}
            WHERE symbol IS NOT NULL AND close > 0
              AND date <= {end} AND date >= {end} - {LOOKBACK_DAYS}
            ORDER BY symbol, date
        ) TO STDOUT WITH (FORMAT csv, HEADER)
    """
    with tempfile.TemporaryFile(mode="w+b") as buf:
        with connection.cursor() as cursor:
            cursor.copy_expert(sql, buf)
        buf.seek(0)
        df = pd.read_csv(
            buf,
            parse_dates=["date"],
            dtype={"symbol": "category", "exchange": "category", "symboltoken": "category"},
        )
    return df.drop_duplicates(["symbol", "date"], keep="last").reset_index(drop=True)


def _data_version():
    table = HistoricStock._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT max(date), count(*) FROM {table}")
        latest, count = cursor.fetchone()
    return f"{latest}:{count}"


# --------------------------------------------------------------------------- indicators

class _Windows:
    """Per-symbol window operations on a frame sorted by (symbol, date).

    Each symbol's rows are contiguous, so a plain shift/rolling over the whole column
    is correct once rows whose window reaches back into the previous symbol are masked.
    """

    def __init__(self, df):
        grouped = df.groupby("symbol", observed=True, sort=False)
        self.keys = df["symbol"]
        self.pos = grouped.cumcount().to_numpy()
        self.size = grouped["close"].transform("size").to_numpy()

    def shift(self, s, n=1):
        return s.shift(n).where(self.pos >= n)

    def rolling(self, s, window, how="mean", **kwargs):
        return getattr(s.rolling(window), how)(**kwargs).where(self.pos >= window - 1)

    def rolling_std(self, s, window):
        # Rolling variance keeps running sums, so a cheap stock right after an expensive
        # one would lose precision; compute it within each symbol instead.
        out = s.groupby(self.keys, observed=True, sort=False).rolling(window).std(ddof=0)
        return out.droplevel(0).reindex(s.index)

    def ewm(self, s, *, span=None, alpha=None, min_periods=0):
        out = (
            s.groupby(self.keys, observed=True, sort=False)
            .ewm(span=span, alpha=alpha, adjust=False, min_periods=min_periods)
            .mean()
        )
        return out.droplevel(0).reindex(s.index)

    def crossed_above(self, a, b, within):
        """True where `a` crossed above `b` (series or constant) within the last `within` bars."""
        prev_b = self.shift(b) if isinstance(b, pd.Series) else b
        cross = ((a > b) & (self.shift(a) <= prev_b)).astype(float)
        return self.rolling(cross, within, "max") == 1


def _swing_lows(w, low, order=SWING_ORDER):
    """Price of the swing low confirmed on each bar (the bar `order` sessions back), NaN elsewhere.

    Matches argrelextrema(np.less, order): the low is strictly below the `order` lows on each side.
    Only bars that already have `order` later bars are confirmed, so nothing looks ahead.
    """
    pivot = w.shift(low, order)
    left = w.shift(w.rolling(low, order, "min"), order + 1)
    right = w.rolling(low, order, "min")
    return pivot.where((pivot < left) & (pivot < right))


def _add_support(snap, df, swing_low, l1):
    """Support touched by the last two candles: earlier swing lows near their lowest low."""
    found = swing_low.notna().to_numpy()
    pivots = pd.DataFrame({"symbol": df["symbol"].to_numpy()[found].astype(str),
                           "level": swing_low.to_numpy()[found]})
    pattern_low = np.fmin(snap["low"].to_numpy(), l1)
    base = pd.DataFrame({"symbol": snap["symbol"], "pattern_low": pattern_low,
                         "tol": SUPPORT_ATR * snap["atr14"]})
    near = pivots.merge(base, on="symbol")
    near = near[(near["level"] - near["pattern_low"]).abs() <= near["tol"]]
    zones = near.groupby("symbol").agg(support_touches=("level", "size"), support_level=("level", "mean"))
    snap["support_touches"] = snap["symbol"].map(zones["support_touches"]).fillna(0).astype(int).to_numpy()
    snap["support_level"] = snap["symbol"].map(zones["support_level"]).to_numpy()
    snap["pct_above_support"] = (snap["close"] / snap["support_level"] - 1) * 100


def _build_snapshot(df):
    """Compute indicators on all candles and return one row per symbol (its latest bar)."""
    w = _Windows(df)
    o, h, l, c, v = (df[k] for k in ("open", "high", "low", "close", "volume"))
    prev_c = w.shift(c)

    sma20 = w.rolling(c, 20)
    sma50 = w.rolling(c, 50)
    sma200 = w.rolling(c, 200)
    ema20 = w.ewm(c, span=20, min_periods=20)
    ema50 = w.ewm(c, span=50, min_periods=50)

    macd = w.ewm(c, span=12, min_periods=26) - w.ewm(c, span=26, min_periods=26)
    macd_signal = w.ewm(macd, span=9, min_periods=9)

    # Wilder's RSI(14)
    delta = c - prev_c
    avg_gain = w.ewm(delta.clip(lower=0), alpha=1 / 14, min_periods=14)
    avg_loss = w.ewm((-delta).clip(lower=0), alpha=1 / 14, min_periods=14)
    rsi14 = 100 - 100 / (1 + avg_gain / avg_loss)

    std20 = w.rolling_std(c, 20)
    bb_lower = sma20 - 2 * std20

    true_range = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr14 = w.ewm(true_range, alpha=1 / 14, min_periods=14)

    vol_avg20_prev = w.shift(w.rolling(v, 20))
    full = {
        "open": o, "high": h, "low": l, "close": c, "volume": v,
        "sma20": sma20, "sma50": sma50, "sma200": sma200, "ema20": ema20, "ema50": ema50,
        "macd": macd, "macd_signal": macd_signal, "macd_hist": macd - macd_signal,
        "rsi14": rsi14, "bb_lower": bb_lower, "bb_mid": sma20, "atr14": atr14,
        "volume_ratio": v / vol_avg20_prev,
        "avg_turnover": w.rolling(c * v, 20),
        "high_20_prev": w.shift(w.rolling(h, 20, "max")),
        "high_52w_prev": w.shift(w.rolling(h, BARS_52W, "max")),
        "ret21": c / w.shift(c, 21) - 1,
        "ret63": c / w.shift(c, 63) - 1,
        "ret126": c / w.shift(c, 126) - 1,
        "ret5_prev": prev_c / w.shift(c, 6) - 1,
        "golden_cross": w.crossed_above(sma50, sma200, 5),
        "ema_cross": w.crossed_above(ema20, ema50, 3),
        "macd_cross": w.crossed_above(macd, macd_signal, 3),
        "rsi_cross30": w.crossed_above(rsi14, 30, 3),
        "bb_dip": w.rolling(w.shift((c < bb_lower).astype(float)), 3, "max") == 1,
    }
    swing_low = _swing_lows(w, l)

    last = np.flatnonzero(w.pos == w.size - 1)
    last_pos = w.pos[last]

    def prior(series, k):
        values = series.to_numpy()[np.maximum(last - k, 0)]
        return np.where(last_pos >= k, values, np.nan)

    snap = pd.DataFrame({name: s.to_numpy()[last] for name, s in full.items()})
    snap.insert(0, "symbol", df["symbol"].to_numpy()[last].astype(str))
    snap.insert(1, "exchange", df["exchange"].to_numpy()[last].astype(str))
    snap.insert(2, "token", df["symboltoken"].to_numpy()[last].astype(str))
    snap.insert(3, "date", df["date"].to_numpy()[last])
    snap["bars"] = last_pos + 1

    o1, h1, l1, c1 = (prior(df[k], 1) for k in ("open", "high", "low", "close"))
    o2, h2, l2, c2 = (prior(df[k], 2) for k in ("open", "high", "low", "close"))
    _add_derived(snap, o1, c1, o2, h2, l2, c2)
    _add_support(snap, df, swing_low, l1)
    return snap


def _add_derived(s, o1, c1, o2, h2, l2, c2):
    o, h, l, c = s["open"], s["high"], s["low"], s["close"]

    s["change_pct"] = (c / c1 - 1) * 100
    s["high_52w"] = np.fmax(s["high_52w_prev"], h)
    s["pct_from_52w_high"] = (c / s["high_52w"] - 1) * 100
    s["close_location"] = np.where(h > l, (c - l) / (h - l), 0.5)
    s["upside_to_mid_pct"] = (s["bb_mid"] / c - 1) * 100
    s["trend_strength_pct"] = (s["sma50"] / s["sma200"] - 1) * 100
    s["stop_loss"] = c - 2 * s["atr14"]
    s["target"] = c + 3 * s["atr14"]
    for n in (21, 63, 126):
        s[f"ret{n}_pct"] = s[f"ret{n}"] * 100

    # Candlestick reversal patterns after a 3%+ drop over the previous five sessions
    body, rng = (c - o).abs(), h - l
    body1, body2, rng2 = np.abs(c1 - o1), np.abs(c2 - o2), h2 - l2
    lower_shadow = np.minimum(o, c) - l
    upper_shadow = h - np.maximum(o, c)
    downtrend = s["ret5_prev"] < -0.03

    hammer = downtrend & (rng > 0) & (lower_shadow >= 2 * body) & (upper_shadow <= 0.25 * rng)
    s["engulfing"] = (c1 < o1) & (c > o) & (c > o1) & (o < c1)
    engulfing = downtrend & s["engulfing"]
    morning_star = (
        downtrend & (c2 < o2) & (body2 >= 0.6 * rng2) & (body1 <= 0.3 * body2)
        & (c > o) & (c > (o2 + c2) / 2)
    )
    s["pattern"] = np.select(
        [morning_star, engulfing, hammer], ["morning star", "bullish engulfing", "hammer"], default=""
    )


class NoCandles(ValueError):
    pass


def get_snapshot(as_of=None):
    """Indicator snapshot of every symbol's last bar (on or before `as_of`), cached per data version."""
    key = f"signals:snapshot:{_data_version()}:{as_of or 'latest'}"
    snap = cache.get(key)
    if snap is None:
        with _compute_lock:
            snap = cache.get(key)
            if snap is None:
                started = time.monotonic()
                candles = _load_candles(as_of)
                if candles.empty:
                    raise NoCandles(f"No candles on or before {as_of}")
                snap = _build_snapshot(candles)
                cache.set(key, snap, CACHE_TIMEOUT)
                logger.info("Built signal snapshot as of %s for %d symbols in %.1fs",
                            as_of or "latest", len(snap), time.monotonic() - started)
    return snap


# --------------------------------------------------------------------------- techniques

@dataclass(frozen=True)
class Technique:
    name: str
    title: str
    description: str
    condition: Callable[[pd.DataFrame], pd.Series]
    score: Callable[[pd.DataFrame], pd.Series]
    columns: tuple = ()
    weight: float = 1.0


TECHNIQUES = {t.name: t for t in [
    Technique(
        "momentum_leaders", "Momentum leaders",
        "Top 15% relative strength (1/3/6-month returns) in a confirmed uptrend: close > SMA50 > SMA200, "
        "within 15% of the 52-week high and RSI below 80.",
        condition=lambda s: (s.rs_score >= 85) & (s.close > s.sma50) & (s.sma50 > s.sma200)
        & (s.pct_from_52w_high >= -15) & (s.rsi14 < 80),
        score=lambda s: s.rs_score,
        columns=("rs_score", "ret21_pct", "ret63_pct", "ret126_pct", "pct_from_52w_high"),
        weight=2,
    ),
    Technique(
        "breakout_52w", "52-week high breakout",
        "Close above the highest high of the previous 252 sessions on at least 1.5x average volume.",
        condition=lambda s: (s.close > s.high_52w_prev) & (s.volume_ratio >= 1.5),
        score=lambda s: s.volume_ratio,
        columns=("high_52w_prev",),
        weight=2,
    ),
    Technique(
        "breakout_52w_ema", "52-week high breakout + EMA trend",
        "52-week high breakout on at least 1.5x average volume, confirmed by the EMA trend: "
        "close > EMA20 > EMA50.",
        condition=lambda s: (s.close > s.high_52w_prev) & (s.volume_ratio >= 1.5)
        & (s.close > s.ema20) & (s.ema20 > s.ema50),
        score=lambda s: s.volume_ratio,
        columns=("high_52w_prev", "ema20", "ema50"),
    ),
    Technique(
        "volume_breakout", "20-day breakout on volume",
        "Bullish candle closing above the previous 20-day high on at least 2x average volume, "
        "in the top quarter of the day's range.",
        condition=lambda s: (s.close > s.high_20_prev) & (s.volume_ratio >= 2) & (s.close > s.open)
        & (s.close_location >= 0.75),
        score=lambda s: s.volume_ratio,
        columns=("high_20_prev",),
        weight=1.5,
    ),
    Technique(
        "pullback_uptrend", "Pullback in uptrend",
        "Uptrend (close > SMA50 > SMA200) where price dipped to the 20-day EMA and closed back above it "
        "on a bullish candle, with RSI between 40 and 60.",
        condition=lambda s: (s.sma50 > s.sma200) & (s.close > s.sma50) & (s.low <= s.ema20)
        & (s.close > s.ema20) & (s.close > s.open) & s.rsi14.between(40, 60),
        score=lambda s: s.trend_strength_pct,
        columns=("ema20", "sma50", "sma200", "trend_strength_pct"),
        weight=1.5,
    ),
    Technique(
        "golden_cross", "Golden cross",
        "SMA50 crossed above SMA200 within the last 5 sessions, with price above SMA50.",
        condition=lambda s: s.golden_cross & (s.close > s.sma50),
        score=lambda s: s.volume_ratio,
        columns=("sma50", "sma200"),
        weight=1.5,
    ),
    Technique(
        "ema_crossover", "EMA 20/50 crossover",
        "EMA20 crossed above EMA50 within the last 3 sessions, with price above EMA20.",
        condition=lambda s: s.ema_cross & (s.close > s.ema20),
        score=lambda s: s.volume_ratio,
        columns=("ema20", "ema50"),
    ),
    Technique(
        "macd_bullish", "MACD bullish crossover",
        "MACD(12, 26, 9) crossed above its signal line within the last 3 sessions. "
        "Crossovers below the zero line (early reversals) rank first.",
        condition=lambda s: s.macd_cross & (s.macd_hist > 0),
        score=lambda s: (s.macd < 0).astype(float) + s.macd_hist / s.atr14,
        columns=("macd", "macd_signal", "macd_hist"),
    ),
    Technique(
        "rsi_reversal", "RSI oversold reversal",
        "RSI(14) climbed back above 30 within the last 3 sessions and is still below 50.",
        condition=lambda s: s.rsi_cross30 & (s.rsi14 < 50),
        score=lambda s: s.volume_ratio,
    ),
    Technique(
        "bollinger_bounce", "Bollinger band bounce",
        "Closed below the lower Bollinger band (20, 2) in one of the previous 3 sessions and is now "
        "back inside the band on a bullish candle.",
        condition=lambda s: s.bb_dip & (s.close > s.bb_lower) & (s.close > s.open),
        score=lambda s: s.upside_to_mid_pct,
        columns=("bb_lower", "bb_mid", "upside_to_mid_pct"),
    ),
    Technique(
        "reversal_candles", "Bullish reversal candles",
        "Hammer, bullish engulfing or morning star on the latest candle after a 3%+ fall over the "
        "previous 5 sessions.",
        condition=lambda s: s.pattern != "",
        score=lambda s: s.volume_ratio,
        columns=("pattern",),
    ),
    Technique(
        "engulfing_support", "Bullish engulfing at support",
        f"Bullish engulfing candle whose low (or the previous candle's low) is within {SUPPORT_ATR} ATR of "
        f"a support level made by at least {MIN_TOUCHES} earlier swing lows, closing above that support.",
        condition=lambda s: s.engulfing & (s.support_touches >= MIN_TOUCHES) & (s.close > s.support_level),
        score=lambda s: s.support_touches + s.volume_ratio,
        columns=("support_level", "support_touches", "pct_above_support"),
        weight=1.5,
    ),
]}

_CROSS_FLAGS = ("golden_cross", "ema_cross", "macd_cross", "rsi_cross30", "bb_dip", "engulfing")

COMMON_COLUMNS = (
    "symbol", "exchange", "token", "date", "close", "change_pct", "rsi14", "volume_ratio",
    "avg_turnover", "atr14", "stop_loss", "target",
)


def _universe(*, min_price, min_avg_turnover, as_of=None):
    """Snapshot rows that are fresh, liquid and have enough history, plus relative strength."""
    snap = get_snapshot(as_of)
    as_of = snap["date"].max()
    s = snap[
        (snap["date"] >= as_of - pd.Timedelta(days=STALE_DAYS))
        & (snap["bars"] >= MIN_BARS)
        & (snap["close"] >= min_price)
        & (snap["avg_turnover"] >= min_avg_turnover)
    ].copy()
    ranks = s[["ret21", "ret63", "ret126"]].rank(pct=True)
    s["rs_score"] = ranks.mean(axis=1, skipna=False) * 100
    return s, as_of


def _clean(value):
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return round(float(value), 2) if np.isfinite(value) else None
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return pd.Timestamp(value).date().isoformat()
    return value


def _records(frame, columns):
    return [{k: _clean(v) for k, v in row.items()} for row in frame[list(columns)].to_dict("records")]


def _matches(technique, s):
    return technique.condition(s).fillna(False).astype(bool)


def list_techniques():
    return [
        {"name": t.name, "title": t.title, "description": t.description, "weight": t.weight}
        for t in TECHNIQUES.values()
    ]


def run_technique(name, *, limit, min_price, min_avg_turnover, as_of=None):
    technique = TECHNIQUES[name]
    s, as_of = _universe(min_price=min_price, min_avg_turnover=min_avg_turnover, as_of=as_of)
    hits = s[_matches(technique, s)].copy()
    hits["score"] = technique.score(hits)
    hits = hits.sort_values("score", ascending=False, na_position="last")
    columns = COMMON_COLUMNS + tuple(c for c in technique.columns if c not in COMMON_COLUMNS) + ("score",)
    return {
        "technique": technique.name,
        "title": technique.title,
        "description": technique.description,
        "as_of": _clean(as_of),
        "universe": len(s),
        "count": len(hits),
        "results": _records(hits.head(limit), columns),
    }


def _composite(s):
    """Weighted count of techniques firing, adjusted for trend, volume and overbought RSI."""
    fired = pd.DataFrame({name: _matches(t, s) for name, t in TECHNIQUES.items()}, index=s.index)
    weights = pd.Series({name: t.weight for name, t in TECHNIQUES.items()})
    score = (
        fired.mul(weights).sum(axis=1)
        + (s["close"] > s["sma200"]) * 1.0
        + (s["volume_ratio"] >= 1.5) * 0.5
        - (s["rsi14"] > 75) * 1.5
    )
    signals = fired.apply(lambda row: [name for name, hit in row.items() if hit], axis=1)
    return score, fired.sum(axis=1), signals


def top_picks(*, limit, min_signals, min_price, min_avg_turnover, as_of=None):
    s, as_of = _universe(min_price=min_price, min_avg_turnover=min_avg_turnover, as_of=as_of)
    s["score"], s["signal_count"], s["signals"] = _composite(s)
    picks = s[s["signal_count"] >= min_signals].sort_values(
        ["score", "volume_ratio"], ascending=False, na_position="last"
    )
    return {
        "technique": "top_picks",
        "title": "Top picks",
        "description": "Stocks where several techniques agree, ranked by a weighted score "
                       "(+1 above SMA200, +0.5 on 1.5x volume, -1.5 if RSI > 75).",
        "as_of": _clean(as_of),
        "universe": len(s),
        "count": len(picks),
        "results": _records(picks.head(limit), COMMON_COLUMNS + ("signal_count", "signals", "score")),
    }


def stock_signals(symbol):
    """Every indicator and technique result for one symbol, or None if it has no data."""
    snap = get_snapshot()
    row = snap[snap["symbol"].str.upper() == symbol.upper()]
    if row.empty:
        return None

    # Relative strength is ranked against every fresh symbol, with no liquidity filter.
    universe, as_of = _universe(min_price=0, min_avg_turnover=0)
    s = universe[universe["symbol"] == row["symbol"].iloc[0]]
    if s.empty:  # too little history or stale: report indicators only
        s = row.assign(rs_score=np.nan)

    score, count, signals = _composite(s)
    return {
        "as_of": _clean(as_of),
        "indicators": _records(s, [c for c in s.columns if c not in _CROSS_FLAGS])[0],
        "signals": signals.iloc[0],
        "signal_count": int(count.iloc[0]),
        "score": _clean(score.iloc[0]),
    }
