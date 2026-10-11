"""Check saved buy signals against the candles stored after the signal date."""
from datetime import date

from django.db import connection
from django.contrib.postgres.aggregates import ArrayAgg
from django.db.models import Avg, Count, Max, Q
from django.utils import timezone

from stocks.models import HistoricStock, TrackedSignal


class UnknownSymbol(ValueError):
    pass


def resolve_instrument(symbol, exchange=""):
    """Find the stored symbol/exchange/token for a typed symbol, e.g. 'reliance' -> RELIANCE-EQ (NSE).

    Returns (symbol, exchange, symboltoken, latest_candle_date).
    """
    # BSE stores 'TCS', NSE stores 'TCS-EQ'; take whichever has the most recent candle.
    qs = HistoricStock.objects.filter(Q(symbol__iexact=symbol) | Q(symbol__iexact=f"{symbol}-EQ"))
    if exchange:
        qs = qs.filter(exchange__iexact=exchange)
    row = qs.order_by("-date").values("symbol", "exchange", "symboltoken", "date").first()
    if row:
        return row["symbol"], row["exchange"] or "", row["symboltoken"], row["date"]
    where = f" on {exchange.upper()}" if exchange else ""
    raise UnknownSymbol(f"No candles stored for '{symbol}'{where}")


def _candles_after(signal):
    """Daily (date, high, low, close) for the signal's instrument after the signal date, oldest first."""
    if signal.symboltoken:
        where, params = "symboltoken = %s", [signal.symboltoken]
    else:
        where, params = "upper(symbol) = upper(%s)", [signal.symbol]
    if signal.exchange:
        where += " AND exchange = %s"  # tokens are only unique within an exchange
        params.append(signal.exchange)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT DISTINCT ON (date) date, high, low, close
            FROM {HistoricStock._meta.db_table}
            WHERE {where} AND date > %s
            ORDER BY date
            """,
            [*params, signal.signal_date],
        )
        return cursor.fetchall()


def verify(signal, save=True):
    """Walk the candles after the signal date until the target or stop loss is touched.

    A candle whose high reaches the target counts as a hit on that date. If the same candle
    also touches the stop loss we can't tell which came first from daily data, so it is
    counted as a stop loss hit (the conservative reading).
    """
    candles = _candles_after(signal)
    status, hit_date, days = TrackedSignal.OPEN, None, len(candles)
    max_high = min_low = None
    for i, (date, high, low, _close) in enumerate(candles, start=1):
        max_high = high if max_high is None else max(max_high, high)
        min_low = low if min_low is None else min(min_low, low)
        if signal.stop_loss is not None and low <= signal.stop_loss:
            status, hit_date, days = TrackedSignal.STOP_HIT, date, i
            break
        if high >= signal.target:
            status, hit_date, days = TrackedSignal.TARGET_HIT, date, i
            break

    checked_through = candles[-1][0] if candles else None
    end_date = hit_date or checked_through
    signal.status = status
    signal.hit_date = hit_date
    signal.trading_days = days if candles else None
    signal.calendar_days = (end_date - signal.signal_date).days if end_date else None
    signal.max_high = max_high
    signal.min_low = min_low
    signal.last_close = candles[-1][3] if candles else None
    signal.checked_through = checked_through
    signal.verified_at = timezone.now()
    if save:
        signal.save()
    return signal


def verify_all(queryset=None):
    signals = list(queryset if queryset is not None else TrackedSignal.objects.all())
    for signal in signals:
        verify(signal)
    return signals


RECOMMENDATION = "recommendation"


def save_recommendations(result):
    """Save the picks of a recommend() result as tracked signals under their candle date.

    Picks already saved for the same stock and date are skipped. Past-date picks are verified
    right away, so their outcome is stored too.
    """
    signal_date = date.fromisoformat(result["as_of"])
    existing = set(TrackedSignal.objects.filter(technique=RECOMMENDATION, signal_date=signal_date)
                   .values_list("symboltoken", "exchange"))
    new = [
        TrackedSignal(
            symbol=p["symbol"], exchange=p["exchange"], symboltoken=p["token"], technique=RECOMMENDATION,
            signal_date=signal_date, buy_price=p["buy_price"], target=p["target"], stop_loss=p["stop_loss"],
            notes=f"#{p['rank']} " + ", ".join(p["signals"]) + " | " + " ".join(p["why"][:2]),
        )
        for p in result["recommendations"] if (p["token"], p["exchange"]) not in existing
    ]
    TrackedSignal.objects.bulk_create(new)
    verify_all(new)
    return {"saved": len(new), "already_saved": len(result["recommendations"]) - len(new),
            "signal_date": result["as_of"]}


def summary(queryset=None):
    """Hit rate and average time to target across tracked signals (all, or the given queryset)."""
    qs = TrackedSignal.objects.all() if queryset is None else queryset
    agg = qs.aggregate(
        total=Count("id"),
        open=Count("id", filter=Q(status=TrackedSignal.OPEN)),
        target_hit=Count("id", filter=Q(status=TrackedSignal.TARGET_HIT)),
        stop_hit=Count("id", filter=Q(status=TrackedSignal.STOP_HIT)),
        avg_trading_days_to_target=Avg("trading_days", filter=Q(status=TrackedSignal.TARGET_HIT)),
        avg_calendar_days_to_target=Avg("calendar_days", filter=Q(status=TrackedSignal.TARGET_HIT)),
    )
    closed = agg["target_hit"] + agg["stop_hit"]
    agg["hit_rate_pct"] = round(agg["target_hit"] / closed * 100, 1) if closed else None
    for key in ("avg_trading_days_to_target", "avg_calendar_days_to_target"):
        if agg[key] is not None:
            agg[key] = round(agg[key], 1)
    return agg


def summary_by_date(queryset=None):
    """One row per prediction date: how many were saved and how they turned out."""
    qs = TrackedSignal.objects.all() if queryset is None else queryset
    rows = (
        qs.values("signal_date")
        .annotate(
            total=Count("id"),
            open=Count("id", filter=Q(status=TrackedSignal.OPEN)),
            target_hit=Count("id", filter=Q(status=TrackedSignal.TARGET_HIT)),
            stop_hit=Count("id", filter=Q(status=TrackedSignal.STOP_HIT)),
            avg_trading_days_to_target=Avg("trading_days", filter=Q(status=TrackedSignal.TARGET_HIT)),
            checked_through=Max("checked_through"),
            techniques=ArrayAgg("technique", distinct=True),
        )
        .order_by("-signal_date")
    )
    out = []
    for r in rows:
        closed = r["target_hit"] + r["stop_hit"]
        r["hit_rate_pct"] = round(r["target_hit"] / closed * 100, 1) if closed else None
        if r["avg_trading_days_to_target"] is not None:
            r["avg_trading_days_to_target"] = round(r["avg_trading_days_to_target"], 1)
        out.append(r)
    return out


def summary_by_technique(queryset):
    return {
        technique: summary(queryset.filter(technique=technique))
        for technique in queryset.order_by("technique").values_list("technique", flat=True).distinct()
    }
