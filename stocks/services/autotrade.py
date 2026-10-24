"""Automatic paper trader: picks stocks with the app's own rules and paper-trades them, so every
prediction is saved with its technique, buy time, target and whether (and how fast) it got there.

Run `python manage.py paper_autotrade` on a trading day (it loops until Ctrl+C). Each cycle:

1. Exits for every book: stops, targets and the 15:15 intraday square-off (paper.book).
2. Intraday, during the money-flow signal window: breakout signals that meet every rule
   (stocks.analysis.flow_strategy) are paper-bought with their stop and target. One per stock per
   day, at most INTRADAY_MAX_PER_DAY.
3. Short-term, from 09:30: today's recommendations re-checked at the live price
   (stocks.analysis.live_signals); when one turns BUY it is paper-bought with its plan's stop and
   target. Once per stock per plan date, at most SHORT_TERM_MAX_PER_DAY.
4. Long-term, once a day from LONG_TERM_FROM: recommendations in a long-term uptrend (above the
   200-day average) that fired a trend technique (LONG_TERM_TECHNIQUES) are paper-bought at the
   live price, stop LONG_STOP_ATR x ATR below and target LONG_TARGET_ATR x ATR above. Skipped while
   the stock is already held in the long-term book; at most LONG_TERM_MAX_PER_DAY.

Sizes come from risk.size (MAX_LOSS_PER_TRADE) within each book's cash. Each cycle writes
DATA_DIR/autotrade_status.json, which the dashboard shows. The dashboard can also start the trader
as a background process (launch) and stop it (request_stop writes a flag file the loop checks
every second). Paper only: no real order is ever
placed. The intraday rules are not proven in their backtest; paper-trading them is how they
collect evidence.
"""
import json
import logging
import os
import subprocess
import sys
import time
from collections import deque
from datetime import time as dtime

from django.conf import settings
from django.utils import timezone

from stocks.analysis import flow_strategy, live_signals
from stocks.models import PaperTrade
from stocks.services import money_flow, paper

logger = logging.getLogger(__name__)

CYCLE_SECONDS = 30
MARKET_OPEN, SETTLED, MARKET_CLOSE = dtime(9, 15), dtime(9, 30), dtime(15, 30)
INTRADAY_MAX_PER_DAY = 5
SHORT_TERM_MAX_PER_DAY = 3
LONG_TERM_MAX_PER_DAY = 2
LONG_TERM_FROM = dtime(10, 0)
LONG_TERM_TECHNIQUES = {"momentum_leaders", "golden_cross", "breakout_52w", "breakout_52w_ema", "pullback_uptrend"}
LONG_STOP_ATR, LONG_TARGET_ATR = 4, 10
FLOW_STOCKS = 100
PICKS = 10
LOG_SIZE = 200

_status = {"started_at": None, "last_cycle": None, "cycles": 0, "long_term_done": None,
           "log": deque(maxlen=LOG_SIZE)}


def status_path():
    return settings.DATA_DIR / "autotrade_status.json"


def load_status():
    """The status the trader last saved, plus whether it looks alive."""
    try:
        data = json.loads(status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"running": False, "log": []}
    data["running"] = time.time() - data.get("last_cycle_epoch", 0) < 3 * data.get("cycle_seconds", CYCLE_SECONDS)
    return data


def _save_status():
    data = {**_status, "log": list(_status["log"])[::-1], "cycle_seconds": CYCLE_SECONDS,
            "last_cycle_epoch": time.time(),
            "rules": {"intraday_max_per_day": INTRADAY_MAX_PER_DAY, "short_term_max_per_day": SHORT_TERM_MAX_PER_DAY,
                      "long_term_max_per_day": LONG_TERM_MAX_PER_DAY, "long_term_techniques": sorted(LONG_TERM_TECHNIQUES),
                      "long_term_stop_atr": LONG_STOP_ATR, "long_term_target_atr": LONG_TARGET_ATR}}
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")


def _log(kind, text):
    now = timezone.localtime()
    _status["log"].append({"time": now.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "text": text})
    logger.info("Autotrade %s: %s", kind, text)


def _auto_today(book, day):
    return PaperTrade.objects.filter(book=book, auto=True, entry_at__date=day).count()


def _buy(book, symbol, *, stop_loss, target, technique, signal, signal_date, source):
    try:
        t = paper.place(book=book, symbol=symbol, stop_loss=round(stop_loss, 2), target=round(target, 2),
                        technique=technique, auto=True, signal=signal, signal_date=signal_date, source=source)
    except paper.PaperError as exc:
        _log("skip", f"{book} {symbol}: {exc}")
        return None
    _log("buy", f"{book}: bought {t.qty} {t.symbol} at {t.entry_price}, stop {t.stop_loss}, target {t.target} "
                f"({technique})")
    return t


# ---------- the three books ----------

def _intraday(now):
    if not flow_strategy.FIRST_SIGNAL <= now.time() <= flow_strategy.LAST_SIGNAL:
        return
    left = INTRADAY_MAX_PER_DAY - _auto_today(PaperTrade.INTRADAY, now.date())
    if left <= 0:
        return
    st = money_flow.money_flow(FLOW_STOCKS, fno=False)["strategy"]
    for s in st.get("signals", []):
        if left <= 0:
            break
        if s["action"] != "SIGNAL" or PaperTrade.objects.filter(
                book=PaperTrade.INTRADAY, symbol=s["symbol"], entry_at__date=now.date()).exists():
            continue
        signal = {k: s[k] for k in ("relative_value", "change_pct", "vwap", "opening_high", "ltp")}
        signal.update(rules=st["params"], checks=[f"{c['name']}: {c['detail']}" for c in s["checks"]])
        if _buy(PaperTrade.INTRADAY, s["symbol"], stop_loss=s["stop_loss"], target=s["target"],
                technique="money_flow_breakout", signal=signal, signal_date=now.date(), source="auto:money_flow"):
            left -= 1


def _short_term(now):
    if now.time() < SETTLED:
        return
    left = SHORT_TERM_MAX_PER_DAY - _auto_today(PaperTrade.SHORT_TERM, now.date())
    if left <= 0:
        return
    out = live_signals.equity_suggestions(PICKS)
    for r in out["suggestions"]:
        if left <= 0:
            break
        if r["action"] != "BUY" or PaperTrade.objects.filter(
                book=PaperTrade.SHORT_TERM, symbol=r["symbol"], signal_date=out["plan_as_of"]).exists():
            continue
        sim = (r.get("simulation") or {})
        signal = {"why": r["why_picked"], "risks": r["risks"], "planned_buy": r["planned_buy"],
                  "max_buy_price": r["max_buy_price"], "checks": [f"{c['name']}: {c['detail']}" for c in r["checks"]],
                  "time_machine": sim.get("verdict")}
        if _buy(PaperTrade.SHORT_TERM, r["symbol"], stop_loss=r["stop_loss"], target=r["target"],
                technique="+".join(sorted(r["signals"])) or "recommendation", signal=signal,
                signal_date=out["plan_as_of"], source="auto:recommendation"):
            left -= 1


def _long_term(now):
    if now.time() < LONG_TERM_FROM or _status["long_term_done"] == now.date():
        return
    plan = live_signals._equity_picks(PICKS)
    held = set(PaperTrade.objects.filter(book=PaperTrade.LONG_TERM, status=PaperTrade.OPEN)
               .values_list("symbol", flat=True))
    bought = _auto_today(PaperTrade.LONG_TERM, now.date())
    for p in plan["recommendations"]:
        if bought >= LONG_TERM_MAX_PER_DAY:
            break
        trend = LONG_TERM_TECHNIQUES & set(p["signals"] + p["other_signals"])
        if not trend or not p.get("above_200dma") or p["symbol"] in held:
            continue
        ltp, _ = paper._ltp(p["exchange"], p["token"])
        if not ltp:
            continue
        signal = {"why": p["why"], "atr": p["atr"], "plan_close": p["buy_price"],
                  "rule": f"above the 200-day average with {', '.join(sorted(trend))}; "
                          f"stop {LONG_STOP_ATR} ATR, target {LONG_TARGET_ATR} ATR"}
        if _buy(PaperTrade.LONG_TERM, p["symbol"], stop_loss=ltp - LONG_STOP_ATR * p["atr"],
                target=ltp + LONG_TARGET_ATR * p["atr"], technique="+".join(sorted(trend)), signal=signal,
                signal_date=plan["as_of"], source="auto:long_term"):
            bought += 1
    _status["long_term_done"] = now.date()


# ---------- the loop ----------

def cycle():
    """One pass: exits for every book, then new entries while the market is open."""
    now = timezone.localtime()
    before = {b: set(PaperTrade.objects.filter(book=b, status=PaperTrade.OPEN).values_list("id", flat=True))
              for b, _ in PaperTrade.BOOK_CHOICES}
    for b, _ in PaperTrade.BOOK_CHOICES:
        paper.book(b)  # runs the stops, targets and square-off
        for t in PaperTrade.objects.filter(id__in=before[b], status=PaperTrade.CLOSED):
            _log("exit", f"{b}: {t.symbol} closed at {t.exit_price} ({t.exit_reason}), "
                         f"P&L {t.pnl:+,.2f} after costs ({t.technique})")
    if now.weekday() < 5 and MARKET_OPEN <= now.time() < MARKET_CLOSE:
        for step in (_intraday, _short_term, _long_term):
            try:
                step(now)
            except Exception as exc:  # one book failing must not stop the others
                logger.exception("Autotrade %s failed", step.__name__)
                _log("error", f"{step.__name__.strip('_')}: {exc}")
    _status["last_cycle"] = now.strftime("%Y-%m-%d %H:%M:%S")
    _status["cycles"] += 1
    _save_status()


def _stop_flag():
    return settings.DATA_DIR / "autotrade.stop"


def launch():
    """Start `manage.py paper_autotrade` as a background process that outlives the web request."""
    if load_status()["running"]:
        raise RuntimeError("The automatic paper trader is already running")
    _stop_flag().unlink(missing_ok=True)
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    log = open(settings.DATA_DIR / "autotrade.log", "ab")
    command = [sys.executable, str(settings.BASE_DIR / "manage.py"), "paper_autotrade"]
    options = dict(cwd=settings.BASE_DIR, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    try:
        if os.name != "nt":
            return subprocess.Popen(command, start_new_session=True, **options).pid
        # Windows kills a child together with the job (terminal / server) that started it unless it
        # breaks away; some jobs forbid that, so fall back to a plain detached process.
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            return subprocess.Popen(command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **options).pid
        except OSError:
            logger.warning("Could not break away from the job; the trader stops when the web server stops")
            return subprocess.Popen(command, creationflags=flags, **options).pid
    finally:
        log.close()


def request_stop():
    """Ask a running trader to stop after its current cycle; False if none is running."""
    if not load_status()["running"]:
        return False
    _stop_flag().touch()
    return True


def stop():
    """Mark the trader as stopped, so the dashboard does not wait for its next cycle."""
    _log("stop", "Automatic paper trader stopped")
    _save_status()
    data = json.loads(status_path().read_text(encoding="utf-8"))
    data["last_cycle_epoch"] = 0
    status_path().write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")


def run(interval=CYCLE_SECONDS, once=False):
    global CYCLE_SECONDS
    CYCLE_SECONDS = interval
    _status["started_at"] = timezone.localtime().strftime("%Y-%m-%d %H:%M:%S")
    _status["pid"] = os.getpid()
    _stop_flag().unlink(missing_ok=True)
    _log("start", f"Automatic paper trader started, a cycle every {interval}s")
    _save_status()  # visible as running before the first (slow) cycle ends
    while True:
        started = time.monotonic()
        try:
            cycle()
        except Exception as exc:
            logger.exception("Autotrade cycle failed")
            _log("error", str(exc))
            _save_status()
        if once:
            return
        while time.monotonic() - started < interval:
            if _stop_flag().exists():
                _stop_flag().unlink(missing_ok=True)
                stop()
                return
            time.sleep(1)
