"""Runs the historic candle fetch in a background thread and tracks its progress.

State lives in this process, so live progress is only visible to the server process
that started the fetch (fine for `runserver` or a single worker). The result of the last
finished run is also saved to DATA_DIR/last_fetch.json so it survives restarts.
"""
import json
import logging
import threading
import time

from django.conf import settings
from django.db import connections

from stocks.services import historic

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_job = None
_cancel_event = None


class FetchAlreadyRunning(RuntimeError):
    pass


def start(start_date, end_date, *, tokens=None, auto_import=False):
    """Start a fetch in the background; raises FetchAlreadyRunning if one is in progress.

    `tokens` limits the fetch to those instruments; `auto_import` inserts the CSV into
    historicstocks when the run ends, including a run stopped early.
    """
    global _job, _cancel_event
    with _lock:
        if _job is not None and _job["status"] == "running":
            raise FetchAlreadyRunning("A historic fetch is already running")
        _cancel_event = threading.Event()
        _job = {
            "status": "running",
            "start_date": start_date,
            "end_date": end_date,
            "started_at": time.time(),
            "finished_at": None,
            "total": len(tokens) if tokens is not None else 0, "fetched": 0, "failed": 0, "rows": 0,
            "only_missing": tokens is not None,
            "auto_import": auto_import,
            "phase": "fetching",
            "imported_rows": None,
            "summary": None,
            "error": None,
        }
        threading.Thread(
            target=_run, args=(start_date, end_date, _cancel_event, tokens, auto_import),
            name="historic-fetch", daemon=True,
        ).start()
    return progress()


def cancel():
    """Ask the running fetch to stop; returns False if nothing is running."""
    with _lock:
        if _job is None or _job["status"] != "running":
            return False
        _cancel_event.set()
        return True


def _last_fetch_path():
    return settings.DATA_DIR / "last_fetch.json"


def _load_last_fetch():
    try:
        return json.loads(_last_fetch_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_last_fetch(state):
    path = _last_fetch_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.exception("Could not save %s", path)


def progress():
    """Current job state plus derived percent, elapsed time, rate and ETA.

    With no job in this process, returns the last saved run, or {"status": "idle"}.
    """
    with _lock:
        if _job is None:
            return _load_last_fetch() or {"status": "idle"}
        job = dict(_job)

    end = job["finished_at"] or time.time()
    elapsed = end - job["started_at"]
    done = job["fetched"] + job["failed"]
    rate = done / elapsed if elapsed > 0 else 0
    remaining = max(job["total"] - done, 0)
    job.update(
        processed=done,
        percent=round(done / job["total"] * 100, 1) if job["total"] else 0.0,
        elapsed_seconds=round(elapsed),
        per_second=round(rate, 2),
        eta_seconds=round(remaining / rate) if job["status"] == "running" and rate > 0 else None,
    )
    return job


def _update(**fields):
    with _lock:
        _job.update(fields)


def _run(start_date, end_date, cancel_event, tokens=None, auto_import=False):
    try:
        summary = historic.fetch_historic_to_csv(
            start_date, end_date,
            tokens=tokens,
            on_progress=lambda counts: _update(**counts),
            cancel_event=cancel_event,
        )
        if summary["cancelled"]:
            status = "cancelled"
        elif summary["stopped"]:
            status = "stopped"
        else:
            status = "completed"
        if auto_import and summary["csv_file"]:
            _update(phase="importing")
            try:
                summary["imported_rows"] = historic.import_historic_csv(historic.resolve_csv(summary["csv_file"]))
                _update(imported_rows=summary["imported_rows"])
            except Exception as exc:
                logger.exception("Import of %s failed", summary["csv_file"])
                summary["import_error"] = str(exc)
        _update(status=status, phase="done", summary=summary, error=summary["stopped_reason"], finished_at=time.time())
    except Exception as exc:
        logger.exception("Historic fetch failed")
        _update(status="error", error=str(exc), finished_at=time.time())
    finally:
        connections.close_all()  # this thread opened its own DB connection
    _save_last_fetch(progress())
