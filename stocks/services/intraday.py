"""5-minute candles for the money-flow strategy, one gzipped CSV per stock in DATA_DIR/intraday.

Angel One serves up to 100 days of 5-minute candles per request, so the first download asks for
the last DAYS calendar days per stock and later runs only for the days after the newest stored
candle (the newest day is fetched again, in case it was stored mid-session). About a second per
stock. Runs in a background thread with progress, like backtest_jobs: live progress is only
visible to the server process that started it.
"""
import json
import logging
import threading
import time
from datetime import timedelta

import pandas as pd
from django.conf import settings
from django.db import connections
from django.utils import timezone

from stocks.models import Instrument
from stocks.services import historic, smartapi

logger = logging.getLogger(__name__)

DAYS = 95          # calendar days on the first download (Angel One's limit is 100 per request)
KEEP_DAYS = 180    # older candles are dropped from the files
INTERVAL = "FIVE_MINUTE"
COLUMNS = ["time", "open", "high", "low", "close", "volume"]

_lock = threading.Lock()
_job = None
_cancel_event = None


class DownloadAlreadyRunning(RuntimeError):
    pass


def _dir():
    return settings.DATA_DIR / "intraday"


def _path(token):
    return _dir() / f"NSE-{token}.csv.gz"


def _index_path():
    return _dir() / "stocks.json"


def stored_stocks():
    """{token: symbol} of every stock with stored 5-minute candles."""
    try:
        index = json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {token: symbol for token, symbol in index.items() if _path(token).exists()}


def load(token):
    """The stored 5-minute candles of one NSE token (naive India time), or None."""
    path = _path(token)
    if not path.exists():
        return None
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"])
    return df


def _save(token, df):
    path = _path(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, compression="gzip")


def _update_stock(client, symbol, token, today, stop_event):
    """Fetch the missing days of one stock; returns the number of stored candles, or None on failure."""
    old = load(token)
    start = today - timedelta(days=DAYS) if old is None else old["time"].max().date()
    inst = Instrument(token=token, exch_seg="NSE", symbol=symbol)  # not saved: only the fetch reads it
    df = historic.fetch_candles(client, inst, f"{start} 09:15", f"{today} 15:30", stop_event, interval=INTERVAL)
    if df is None:
        return None
    df = df.rename(columns={"date": "time"})[COLUMNS]
    # "2026-10-23T09:15:00+05:30" -> naive India time
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(settings.TIME_ZONE).dt.tz_localize(None)
    if old is not None:
        df = pd.concat([old, df]).drop_duplicates("time", keep="last")
    df = df[df["time"] >= pd.Timestamp(today - timedelta(days=KEEP_DAYS))].sort_values("time")
    _save(token, df)
    return len(df)


def start(stocks):
    """Download the missing candles of `stocks` ([{"symbol", "token"}]) in the background."""
    global _job, _cancel_event
    with _lock:
        if _job is not None and _job["status"] == "running":
            raise DownloadAlreadyRunning("The 5-minute candle download is already running")
        index = stored_stocks()
        index.update({s["token"]: s["symbol"] for s in stocks})
        _dir().mkdir(parents=True, exist_ok=True)
        _index_path().write_text(json.dumps(index, indent=0), encoding="utf-8")
        _cancel_event = threading.Event()
        _job = {"status": "running", "started_at": time.time(), "finished_at": None,
                "done": 0, "total": len(stocks), "failed": [], "current": None, "error": None}
        threading.Thread(target=_run, args=(stocks, _cancel_event), name="intraday-download", daemon=True).start()
    return progress()


def cancel():
    with _lock:
        if _job is None or _job["status"] != "running":
            return False
        _cancel_event.set()
        return True


def progress():
    with _lock:
        if _job is None:
            return {"status": "idle"}
        job = dict(_job, failed=list(_job["failed"]))
    elapsed = (job["finished_at"] or time.time()) - job["started_at"]
    done, total = job["done"], job["total"]
    job.update(percent=round(done / total * 100, 1) if total else 0.0, elapsed_seconds=round(elapsed),
               eta_seconds=round(elapsed / done * (total - done)) if job["status"] == "running" and done else None)
    return job


def _update(**fields):
    with _lock:
        _job.update(fields)


def _run(stocks, cancel_event):
    try:
        client = smartapi.get_client()
        today = timezone.localdate()
        for n, s in enumerate(stocks, start=1):
            if cancel_event.is_set():
                _update(status="cancelled", finished_at=time.time())
                return
            _update(current=s["symbol"])
            try:
                rows = _update_stock(client, s["symbol"], s["token"], today, cancel_event)
            except historic.RateLimitExceeded as exc:
                _update(status="error", error=f"Angel One rate limit: {exc}. Run it again in a few minutes.",
                        finished_at=time.time())
                return
            except Exception:
                logger.exception("5-minute candles for %s failed", s["symbol"])
                rows = None
            with _lock:
                if rows is None:
                    _job["failed"].append(s["symbol"])
                _job["done"] = n
        _update(status="completed", current=None, finished_at=time.time())
    except Exception as exc:
        logger.exception("5-minute candle download failed")
        _update(status="error", error=str(exc), finished_at=time.time())
    finally:
        connections.close_all()
