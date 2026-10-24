"""Today's buy recommendations, ranked only by techniques with a proven edge in the backtest.

A technique counts as proven when, in the saved backtest (see stocks.analysis.backtest), it had
enough trades, beat buying every liquid stock on the same dates, and made more than it lost
(profit factor >= 1). Each stock's score is the sum of the excess returns of the proven
techniques firing on it today. Every pick comes with plain-language reasons and the track record
of its signals. Without a backtest file, the techniques' default weights are used instead.

This is a mechanical screen on past prices, not investment advice.
"""
import math
from datetime import date

from stocks.analysis import backtest, outcomes, risk, signals
from stocks.analysis import simulate as time_machine

MIN_TRADES = 200
MIN_PROFIT_FACTOR = 1.0


def _fmt(v, digits=2):
    return f"{v:,.{digits}f}"


# Why each technique fired, in plain words, using today's indicator values.
REASONS = {
    "rsi_reversal": lambda s: f"RSI(14) has just climbed back above 30 from oversold and is now {s.rsi14:.0f}: "
                              "selling pressure is fading and the stock is starting to bounce.",
    "macd_bullish": lambda s: "MACD has just crossed above its signal line: short-term momentum has turned up"
                              + (" from below zero, an early reversal." if s.macd < 0 else "."),
    "bollinger_bounce": lambda s: f"It dipped below the lower Bollinger band and closed back inside on a green candle, "
                                  f"with {_fmt(s.upside_to_mid_pct, 1)}% room to the middle band.",
    "pullback_uptrend": lambda s: "In an uptrend (price > 50-day > 200-day average) it pulled back to the 20-day EMA "
                                  "and bounced off it.",
    "golden_cross": lambda s: "The 50-day average has just crossed above the 200-day average (golden cross).",
    "ema_crossover": lambda s: "The 20-day EMA has just crossed above the 50-day EMA.",
    "volume_breakout": lambda s: f"It closed above its 20-day high on {_fmt(s.volume_ratio, 1)}x its normal volume.",
    "breakout_52w": lambda s: f"It broke to a new 52-week high on {_fmt(s.volume_ratio, 1)}x its normal volume.",
    "breakout_52w_ema": lambda s: f"It broke to a new 52-week high on {_fmt(s.volume_ratio, 1)}x its normal volume, "
                                  f"with the EMA trend up: close > 20-day EMA ({_fmt(s.ema20)}) > 50-day EMA ({_fmt(s.ema50)}).",
    "momentum_leaders": lambda s: f"It is among the strongest stocks over 1-6 months (relative strength {s.rs_score:.0f}/100).",
    "reversal_candles": lambda s: f"A {s.pattern} candle formed after a 3%+ fall, a common reversal pattern.",
    "engulfing_support": lambda s: f"A bullish engulfing candle formed at support near {_fmt(s.support_level, 2)}, "
                                   f"a level that held {s.support_touches} times before.",
}


def is_proven(st):
    """Whether a technique's backtest stats show an edge worth trading."""
    return st["trades"] >= MIN_TRADES and st["excess_return_pct"] > 0 and (st["profit_factor"] or 0) >= MIN_PROFIT_FACTOR


def _evidence_weights():
    """{technique: weight} for proven techniques, plus the backtest summary (or None)."""
    bt = backtest.load_results()
    if not bt:
        return {name: t.weight for name, t in signals.TECHNIQUES.items()}, None
    weights = {}
    for name, st in bt["techniques"].items():
        if name not in signals.TECHNIQUES:
            continue
        if is_proven(st):
            weights[name] = st["excess_return_pct"]
    return weights, bt


def _track_record(st):
    return (f"In the backtest this signal made {st['avg_return_pct']:+.2f}% per trade on average "
            f"({st['excess_return_pct']:+.2f}% more than buying any stock), won {st['win_rate_pct']:.0f}% of "
            f"{st['trades']:,} trades and beat the market in {st['months_beating_market']} of {st['months']} months.")


def recommend(*, count=3, min_price=20.0, min_avg_turnover=5_000_000, news=False, as_of=None, simulate=False):
    """Recommendations on the latest candle, or as they would have been on `as_of` (candles up to
    that date only), in which case each pick also shows what happened afterwards.

    Each pick is sized to TRADING_CAPITAL / MAX_LOSS_PER_TRADE; with `simulate`, it also gets the
    time machine's odds (stocks.analysis.simulate) from the candles up to the recommendation date."""
    weights, bt = _evidence_weights()
    requested_date = as_of
    s, as_of = signals._universe(min_price=min_price, min_avg_turnover=min_avg_turnover, as_of=as_of)
    s = s[s["atr14"] > 0]

    fired = {name: signals._matches(signals.TECHNIQUES[name], s) for name in weights}
    score = sum((mask * weights[name] for name, mask in fired.items()), start=0 * s["close"])
    others = {name: signals._matches(t, s) for name, t in signals.TECHNIQUES.items() if name not in weights}

    candidates = s[score > 0].assign(_score=score[score > 0])
    # With news, look at a few extra candidates so dropping "avoid" ones still leaves `count`.
    candidates = candidates.sort_values(["_score", "volume_ratio"], ascending=False).head(count * 3 if news else count)
    news_by_symbol, news_info = _news_check(candidates, news)
    if news:
        candidates = candidates[[news_by_symbol.get(sym, {}).get("verdict") != "avoid"
                                 for sym in candidates["symbol"]]].head(count)

    picks = []
    for rank, (idx, row) in enumerate(candidates.iterrows(), start=1):
        proven = [name for name in weights if fired[name].loc[idx]]
        also = [name for name, mask in others.items() if mask.loc[idx]]
        why = [REASONS[name](row) for name in proven]
        if bt:
            why += [f"{signals.TECHNIQUES[name].title}: {_track_record(bt['techniques'][name])}" for name in proven]
        why.append(
            "The longer-term trend is up: price is above its 200-day average." if row.close > row.sma200
            else "Note: price is still below its 200-day average, so this is a bounce, not an established uptrend."
            if not math.isnan(row.sma200) else "Not enough history for a 200-day trend check."
        )
        if row.volume_ratio >= 1.5:
            why.append(f"Volume today is {_fmt(row.volume_ratio, 1)}x normal, so buyers are showing up.")
        n = news_by_symbol.get(row.symbol)
        if n:
            why.append(f"News score {n['news_score']:+.2f} ({n['confidence']} confidence): "
                       + ("; ".join(n["reasons"]) or "no relevant company news."))

        target_pct = (row.target / row.close - 1) * 100
        stop_pct = (row.stop_loss / row.close - 1) * 100
        risks = [f"Exit if it closes below the stop loss {_fmt(row.stop_loss)} ({stop_pct:.1f}%)."]
        if bt:
            worst = max(bt["techniques"][n]["stop_hit_pct"] for n in proven)
            risks.append(f"Even with this signal, the stop loss was hit first in up to {worst:.0f}% of past trades.")
        if row.rsi14 > 70:
            risks.append(f"RSI is {row.rsi14:.0f}, overbought territory.")
        days = [bt["techniques"][n]["avg_days_to_target"] for n in proven if bt and bt["techniques"][n]["avg_days_to_target"]]

        position = risk.size(float(row.close), float(row.stop_loss))
        if position and position["lots"] == 0:
            risks.append(risk.fits_detail(position)[1] + ": skip it or raise the limit.")

        picks.append({
            "rank": rank,
            "symbol": row.symbol,
            "exchange": row.exchange,
            "token": row.token,
            "action": "BUY",
            "buy_price": round(float(row.close), 2),
            "target": round(float(row.target), 2),
            "target_pct": round(target_pct, 1),
            "stop_loss": round(float(row.stop_loss), 2),
            "stop_loss_pct": round(stop_pct, 1),
            "reward_to_risk": round(target_pct / -stop_pct, 2) if stop_pct < 0 else None,
            "expected_days_to_target": round(sum(days) / len(days)) if days else None,
            "position": position,
            "atr": round(float(row.atr14), 2),
            "above_200dma": bool(row.close > row.sma200) if not math.isnan(row.sma200) else None,
            "signals": proven,
            "other_signals": also,
            "score": round(float(row._score), 2),
            "why": why,
            "risks": risks,
            **({"news": n} if n else {}),
        })

    proven_list = [{"technique": n, "title": signals.TECHNIQUES[n].title, "weight": w,
                    **({"track_record": bt["techniques"][n]} if bt else {})}
                   for n, w in sorted(weights.items(), key=lambda kv: -kv[1])]
    result = {
        "as_of": signals._clean(as_of),
        "universe": len(s),
        "candidates": int((score > 0).sum()),
        "recommendations": picks,
        "proven_techniques": proven_list,
        "method": ("Ranks today's stocks by the techniques that beat the market in the backtest "
                   f"(at least {MIN_TRADES} trades, positive excess return, profit factor >= {MIN_PROFIT_FACTOR})."
                   if bt else "No backtest saved yet, so default technique weights are used. "
                              "Run `python manage.py backtest_techniques` for evidence-based ranking."),
        "backtest_period": bt["period"] if bt else None,
        "disclaimer": "Mechanical screen on past prices; past results don't guarantee future returns. Not investment advice.",
    }
    if news_info:
        result["news"] = news_info
    if picks and simulate:
        _add_simulations(result)
    if picks:
        _add_outcomes(result)  # for the latest date this just says "no later candles yet"
    if requested_date and bt and bt["period"]["from"] <= result["as_of"] <= bt["period"]["to"]:
        result["lookahead_warning"] = (
            "This date is inside the backtest period, and the backtest that chose the proven techniques "
            "also saw what happened after it. Outcomes for dates before the backtest ends look better than "
            "a real prediction would have; dates after it are a fair test.")
    if not picks:
        result["message"] = f"No stock showed a proven signal on {result['as_of']}. Waiting is also a valid decision."
    return result


def _news_check(candidates, enabled):
    """Claude's news verdict for each candidate, when requested; returns ({symbol: row}, summary)."""
    if not enabled or candidates.empty:
        return {}, None
    from stocks.services import news

    scan = news.scan(symbols=list(candidates["symbol"]))
    by_symbol = {r["symbol"]: {k: r[k] for k in ("verdict", "news_score", "confidence", "reasons", "key_headline")}
                 for r in scan["results"]}
    dropped = [sym for sym, r in by_symbol.items() if r["verdict"] == "avoid"]
    return by_symbol, {"market": scan["market"], "dropped_for_bad_news": dropped, "cached": scan["cached"]}


def _add_simulations(result):
    """The time machine's odds for each pick, or the reason it could not run."""
    as_of = date.fromisoformat(result["as_of"])
    for p in result["recommendations"]:
        try:
            p["simulation"] = time_machine.simulate(token=p["token"], exchange=p["exchange"], entry=p["buy_price"],
                                                    target=p["target"], stop_loss=p["stop_loss"], as_of=as_of)
        except (time_machine.NotEnoughCandles, ValueError) as exc:
            p["simulation"] = {"error": str(exc)}


def _add_outcomes(result):
    """Did each pick reach its target or stop loss in the candles after the recommendation date, and when."""
    start = date.fromisoformat(result["as_of"])
    candles = outcomes._candles_after({(p["token"], p["exchange"]) for p in result["recommendations"]}, start)
    for p in result["recommendations"]:
        o = outcomes.evaluate(candles.get((p["token"], p["exchange"]), []),
                              p["buy_price"], p["target"], p["stop_loss"], start)
        p["outcome"] = o
        # Plain yes/no answers. None = can't tell yet (no candles after the recommendation date).
        has_data = o["outcome"] != outcomes.NO_DATA
        p["target_hit"] = bool(o["target_date"]) if has_data else None
        p["target_hit_date"] = o["target_date"]
        p["stop_loss_hit"] = bool(o["stop_loss_date"]) if has_data else None
        p["stop_loss_hit_date"] = o["stop_loss_date"]
    states = [p["outcome"]["outcome"] for p in result["recommendations"]]
    result["outcome_summary"] = {k: states.count(k) for k in
                                 (outcomes.TARGET_HIT, outcomes.STOP_HIT, outcomes.OPEN, outcomes.NO_DATA)}
