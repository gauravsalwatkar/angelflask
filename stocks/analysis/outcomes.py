"""What happened after a prediction: for each pick, walk the candles after the prediction
date and find when the target (or the stop loss) was first reached. Nothing is saved.
"""
from collections import defaultdict
from datetime import date

from django.db import connection

from stocks.models import HistoricStock

TARGET_HIT = "target_hit"
STOP_HIT = "stop_hit"
OPEN = "open"            # neither level reached yet
NO_DATA = "no_later_data"  # no candles after the prediction date


def _candles_after(keys, after):
    """{(token, exchange): [(date, high, low, close), ...]} for candles after `after`, oldest first."""
    if not keys:
        return {}
    tokens = sorted({token for token, _ in keys})
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT DISTINCT ON (symboltoken, exchange, date) symboltoken, exchange, date, high, low, close
            FROM {HistoricStock._meta.db_table}
            WHERE symboltoken = ANY(%s) AND date > %s
            ORDER BY symboltoken, exchange, date
            """,
            [tokens, after],
        )
        out = defaultdict(list)
        for token, exchange, day, high, low, close in cursor.fetchall():
            if (token, exchange) in keys:  # tokens are only unique within an exchange
                out[(token, exchange)].append((day, high, low, close))
    return out


def evaluate(candles, buy_price, target, stop_loss, start):
    """Outcome of one pick bought at `buy_price` on `start`, given the candles after it.

    The target counts as reached on the first candle whose high is >= target, the stop loss
    on the first candle whose low is <= stop_loss. If both happen on the same candle, daily
    data can't tell which came first, so the outcome is the stop loss (the cautious reading).
    """
    target_date = stop_date = None
    target_days = stop_days = None
    max_high = None
    for i, (day, high, low, _close) in enumerate(candles, start=1):
        if target_date is None and stop_date is None:
            max_high = high if max_high is None else max(max_high, high)
        if stop_date is None and stop_loss is not None and low <= stop_loss:
            stop_date, stop_days = day, i
        if target_date is None and high >= target:
            target_date, target_days = day, i
        if target_date and (stop_date or stop_loss is None):
            break

    if not candles:
        outcome = NO_DATA
    elif stop_date and (target_date is None or stop_date <= target_date):
        outcome = STOP_HIT
    elif target_date:
        outcome = TARGET_HIT
    else:
        outcome = OPEN

    last_day, last_close = (candles[-1][0], candles[-1][3]) if candles else (None, None)
    pct = lambda v: round((v / buy_price - 1) * 100, 2) if v is not None and buy_price else None
    return {
        "outcome": outcome,
        "target_date": target_date.isoformat() if target_date else None,
        "trading_days_to_target": target_days,
        "calendar_days_to_target": (target_date - start).days if target_date else None,
        "stop_loss_date": stop_date.isoformat() if stop_date else None,
        "trading_days_to_stop": stop_days,
        "max_gain_pct": pct(max_high),  # best high before the outcome was decided
        "last_close": last_close,
        "return_pct": pct(last_close),
        "checked_through": last_day.isoformat() if last_day else None,
    }


def add_outcomes(result):
    """Add an outcome to every pick of a run_technique()/top_picks() result, plus a summary."""
    start = date.fromisoformat(result["as_of"])
    keys = {(r["token"], r["exchange"]) for r in result["results"]}
    candles = _candles_after(keys, start)

    rows = []
    for r in result["results"]:
        outcome = evaluate(candles.get((r["token"], r["exchange"]), []), r["close"], r["target"], r["stop_loss"], start)
        # Put the prediction and its outcome first, then the indicator columns.
        lead = {k: r[k] for k in ("symbol", "exchange", "date", "close", "target", "stop_loss")}
        rows.append({**lead, **outcome, **{k: v for k, v in r.items() if k not in lead}})

    counts = {k: sum(1 for r in rows if r["outcome"] == k) for k in (TARGET_HIT, STOP_HIT, OPEN, NO_DATA)}
    decided = counts[TARGET_HIT] + counts[STOP_HIT]
    days = [r["trading_days_to_target"] for r in rows if r["outcome"] == TARGET_HIT]
    checked = [r["checked_through"] for r in rows if r["checked_through"]]
    return {
        **result,
        "results": rows,
        "outcome_summary": {
            "picks": len(rows),
            **counts,
            "hit_rate_pct": round(counts[TARGET_HIT] / decided * 100, 1) if decided else None,
            "reached_target_ever": sum(1 for r in rows if r["target_date"]),
            "avg_trading_days_to_target": round(sum(days) / len(days), 1) if days else None,
            "checked_through": max(checked) if checked else None,
        },
    }
