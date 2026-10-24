"""Runs the strategy backtest in a background thread and tracks its progress.

Like fetch_jobs, live progress is only visible to the server process that started the run;
the finished result is saved by backtest.run_and_save to DATA_DIR/backtest.json.
"""
import logging
import threading
import time

from django.db import connections

from stocks.analysis import backtest

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_job = None
_cancel_event = None


class BacktestAlreadyRunning(RuntimeError):
    pass


class _Cancelled(Exception):
    pass


def start(*, step, hold, start_date=None, end_date=None):
    """Start a backtest in the background; raises BacktestAlreadyRunning if one is in progress."""
    global _job, _cancel_event
    with _lock:
        if _job is not None and _job["status"] == "running":
            raise BacktestAlreadyRunning("A backtest is already running")
        _cancel_event = threading.Event()
        _job = {
            "status": "running",
            "params": {"step": step, "hold": hold,
                       "start_date": start_date.isoformat() if start_date else None,
                       "end_date": end_date.isoformat() if end_date else None},
            "started_at": time.time(),
            "finished_at": None,
            "phase": "loading candles",
            "dates_done": 0, "dates_total": 0, "current_date": None, "trades": 0,
            "error": None,
        }
        threading.Thread(
            target=_run, args=(step, hold, start_date, end_date, _cancel_event), name="backtest", daemon=True
        ).start()
    return progress()


def cancel():
    """Ask the running backtest to stop; returns False if nothing is running."""
    with _lock:
        if _job is None or _job["status"] != "running":
            return False
        _cancel_event.set()
        return True


def progress():
    """Current job state plus percent, elapsed time and ETA; {"status": "idle"} if none ran."""
    with _lock:
        if _job is None:
            return {"status": "idle"}
        job = dict(_job)
    end = job["finished_at"] or time.time()
    elapsed = end - job["started_at"]
    done, total = job["dates_done"], job["dates_total"]
    job.update(
        percent=round(done / total * 100, 1) if total else 0.0,
        elapsed_seconds=round(elapsed),
        eta_seconds=round(elapsed / done * (total - done)) if job["status"] == "running" and done else None,
    )
    return job


def _update(**fields):
    with _lock:
        _job.update(fields)


def _run(step, hold, start_date, end_date, cancel_event):
    def on_progress(n, total, day, trades):
        if cancel_event.is_set():
            raise _Cancelled
        _update(phase="simulating", dates_done=n, dates_total=total, current_date=str(day), trades=trades)

    try:
        backtest.run_and_save(step, hold, on_progress, start_date=start_date, end_date=end_date)
        _update(status="completed", phase="done", finished_at=time.time())
    except _Cancelled:
        _update(status="cancelled", phase="done", finished_at=time.time())
    except Exception as exc:
        logger.exception("Backtest failed")
        _update(status="error", phase="done", error=str(exc), finished_at=time.time())
    finally:
        connections.close_all()  # this thread opened its own DB connection
