"""Screeners that scan stored candles for support zones and bullish engulfing setups."""
import pandas as pd
from django.db import connection

from stocks.analysis.indicators import compute_rsi
from stocks.analysis.support_zones import find_strong_support_zones
from stocks.models import HistoricStock, Instrument
from stocks.utils import safe_round

OHLCV_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume"]


def _query_df(sql, params):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    return pd.DataFrame(rows, columns=OHLCV_COLUMNS)


def iter_ohlcv(lookback, volume_percentile=None, max_symbols=None):
    """Yield (symbol, DataFrame) of daily candles over `lookback` (a Postgres interval)
    ending at the latest stored candle, so stale data still yields a full window.

    With `volume_percentile`, only symbols that traded above that volume percentile
    at least once in the window are included. `max_symbols` caps the symbol count.
    """
    table = HistoricStock._meta.db_table
    params = [lookback]

    volume_filter = ""
    if volume_percentile is not None:
        volume_filter = (
            "AND volume > (SELECT percentile_cont(%s) WITHIN GROUP (ORDER BY volume) FROM recent)"
        )
        params.append(volume_percentile)

    limit = ""
    if max_symbols is not None:
        limit = "LIMIT %s"
        params.append(max_symbols)

    sql = f"""
        WITH recent AS (
            SELECT symbol, date, open, high, low, close, volume
            FROM {table}
            WHERE date >= (SELECT max(date) FROM {table}) - %s::interval
        ),
        active AS (
            SELECT DISTINCT symbol FROM recent
            WHERE symbol IS NOT NULL {volume_filter}
            ORDER BY symbol
            {limit}
        )
        SELECT r.symbol, r.date, r.open, r.high, r.low, r.close, r.volume
        FROM recent r
        JOIN active a ON a.symbol = r.symbol
        ORDER BY r.symbol, r.date
    """
    df = _query_df(sql, params)
    for symbol, group in df.groupby("symbol", sort=False):
        yield symbol, group.drop(columns="symbol").reset_index(drop=True)


def _passes_zone_filters(zone, distance_pct, *, min_touches, min_strength_score,
                         min_bounce_pct, min_volume_factor, max_distance_pct):
    return (
        zone["touches"] >= min_touches
        and zone["strength_score"] >= min_strength_score
        and zone["avg_bounce_pct"] >= min_bounce_pct
        and zone["volume_factor"] >= min_volume_factor
        and distance_pct <= max_distance_pct
    )


def _distance_pct(current_price, support):
    return abs(current_price - support) / support * 100


def strong_support_zones(*, limit, swing_order, eps_pct, **filters):
    """Strong support zones near the current price, using one year of data."""
    results = {}
    for symbol, df in iter_ohlcv("1 year"):
        if len(df) < 120:
            continue

        current_price = float(df.iloc[-1]["close"])
        strong_zones = []
        for z in find_strong_support_zones(df, swing_order=swing_order, eps_pct=eps_pct):
            support = float(z["support_level"])
            distance_pct = _distance_pct(current_price, support)
            if not _passes_zone_filters(z, distance_pct, **filters):
                continue

            strong_zones.append({
                "symbol": symbol,
                "support_level": support,
                "zone_low": z["zone_low"],
                "zone_high": z["zone_high"],
                "current_price": round(current_price, 2),
                "distance_pct": round(distance_pct, 2),
                "touches": z["touches"],
                "avg_bounce_pct": z["avg_bounce_pct"],
                "volume_factor": z["volume_factor"],
                "strength_score": z["strength_score"],
            })

        if strong_zones:
            results[symbol] = strong_zones[:limit]
    return results


def swing_support_zones(*, limit, swing_order, eps_pct, rsi_period, rsi_oversold,
                        min_trend_score, **filters):
    """Swing-trade (5-21 day) support zones confirmed by RSI and trend, six months of data."""
    results = {}
    for symbol, df in iter_ohlcv("6 months", volume_percentile=0.7):
        if len(df) < 60:
            continue

        current_price = float(df.iloc[-1]["close"])
        current_rsi = compute_rsi(df["close"], rsi_period).iloc[-1]
        ma20 = df["close"].rolling(20).mean().iloc[-1]
        trend_score = (current_price - ma20) / current_price if not pd.isna(ma20) else 0

        # Technical confluence is per symbol, so check it before computing zones.
        if current_rsi > rsi_oversold or trend_score < min_trend_score:
            continue

        strong_zones = []
        for z in find_strong_support_zones(df, swing_order=swing_order, eps_pct=eps_pct):
            support = float(z["support_level"])
            distance_pct = _distance_pct(current_price, support)
            if not _passes_zone_filters(z, distance_pct, **filters):
                continue

            swing_score = (
                z["strength_score"] * 0.4
                + z["avg_bounce_pct"] * 10 * 0.3
                + (100 - distance_pct) * 0.2
                + (40 - current_rsi) / 40 * 0.1
            )
            strong_zones.append({
                "symbol": symbol,
                "support_level": support,
                "zone_low": z["zone_low"],
                "zone_high": z["zone_high"],
                "current_price": round(current_price, 2),
                "distance_pct": round(distance_pct, 2),
                "touches": z["touches"],
                "avg_bounce_pct": round(z["avg_bounce_pct"], 2),
                "volume_factor": round(z["volume_factor"], 2),
                "strength_score": z["strength_score"],
                "rsi": safe_round(current_rsi, 1),
                "trend_score": safe_round(trend_score, 3),
                "swing_score": safe_round(swing_score, 2),
            })

        strong_zones.sort(key=lambda x: x["swing_score"] or 0, reverse=True)
        if strong_zones:
            results[symbol] = strong_zones[:limit]
    return results


def _engulfing_score(prev_candle, curr_candle):
    """Score 0-1 for how well the last candle forms a bullish engulfing pattern."""
    prev_body = abs(prev_candle["close"] - prev_candle["open"])
    curr_body = abs(curr_candle["close"] - curr_candle["open"])

    is_bearish_prev = prev_candle["close"] < prev_candle["open"]
    is_bullish_curr = curr_candle["close"] > curr_candle["open"]
    engulfs_body = (
        curr_candle["open"] < prev_candle["close"] and curr_candle["close"] > prev_candle["open"]
    )
    body_ratio = curr_body / prev_body if prev_body > 0 else 0
    if not engulfs_body:
        engulf_depth = 0
    elif prev_body > 0:
        engulf_depth = min(1.0, (prev_candle["open"] - curr_candle["open"]) / prev_body)
    else:
        engulf_depth = 1.0  # doji fully engulfed

    score = (
        (0.3 if is_bearish_prev else 0)
        + (0.2 if is_bullish_curr else 0)
        + engulf_depth * 0.3
        + (min(2.0, body_ratio) / 2) * 0.2
    )
    return score, body_ratio


def support_bullish_engulfing(*, limit, swing_order, eps_pct, min_engulfing_strength,
                              rsi_period, rsi_oversold, max_symbols=50, **filters):
    """Bullish engulfing on the last candle, sitting on a strong support zone."""
    results = {}
    for symbol, df in iter_ohlcv("6 months", volume_percentile=0.8, max_symbols=max_symbols):
        if len(df) < 60:
            continue

        current_price = float(df.iloc[-1]["close"])
        prev_candle = df.iloc[-2]
        curr_candle = df.iloc[-1]

        engulfing_score, body_ratio = _engulfing_score(prev_candle, curr_candle)
        if engulfing_score < min_engulfing_strength:
            continue

        current_rsi = compute_rsi(df["close"], rsi_period).iloc[-1]
        if current_rsi > rsi_oversold:
            continue

        strong_zones = []
        for z in find_strong_support_zones(df, swing_order=swing_order, eps_pct=eps_pct):
            support = float(z["support_level"])
            distance_pct = _distance_pct(current_price, support)
            if not _passes_zone_filters(z, distance_pct, **filters):
                continue

            trade_score = (
                z["strength_score"] * 0.3
                + (100 - distance_pct) * 0.25
                + engulfing_score * 100 * 0.3
                + (50 - current_rsi) / 50 * 0.15
            )
            strong_zones.append({
                "symbol": symbol,
                "support_level": round(support, 2),
                "zone_low": z["zone_low"],
                "zone_high": z["zone_high"],
                "current_price": round(current_price, 2),
                "distance_pct": round(distance_pct, 2),
                "engulfing_score": round(engulfing_score, 2),
                "body_ratio": round(body_ratio, 2),
                "prev_close": round(float(prev_candle["close"]), 2),
                "curr_open": round(float(curr_candle["open"]), 2),
                "curr_close": round(float(curr_candle["close"]), 2),
                "touches": z["touches"],
                "strength_score": z["strength_score"],
                "rsi": safe_round(current_rsi, 1),
                "trade_score": safe_round(trade_score, 2),
            })

        strong_zones.sort(key=lambda x: x["trade_score"] or 0, reverse=True)
        if strong_zones:
            results[symbol] = strong_zones[:limit]
    return results


def bullish_engulfing_symbols():
    """Symbols whose last two stored candles form a bullish engulfing pattern."""
    table = HistoricStock._meta.db_table
    sql = f"""
        WITH ranked AS (
            SELECT symboltoken, date, open, close,
                   ROW_NUMBER() OVER (PARTITION BY symboltoken ORDER BY date DESC) AS rn
            FROM {table}
        ),
        last_two AS (
            SELECT symboltoken, open, close,
                   LAG(open)  OVER (PARTITION BY symboltoken ORDER BY date) AS prev_open,
                   LAG(close) OVER (PARTITION BY symboltoken ORDER BY date) AS prev_close
            FROM ranked
            WHERE rn <= 2
        )
        SELECT DISTINCT symboltoken
        FROM last_two
        WHERE prev_close < prev_open      -- previous candle bearish
          AND close > open                -- current candle bullish
          AND open <= prev_close          -- engulfs lower bound
          AND close >= prev_open          -- engulfs upper bound
    """
    with connection.cursor() as cursor:
        cursor.execute(sql)
        tokens = [row[0] for row in cursor.fetchall()]

    return list(
        Instrument.objects.filter(token__in=tokens).order_by("symbol").values_list("symbol", flat=True)
    )
