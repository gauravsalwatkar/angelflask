"""Fetch daily candles from SmartAPI to CSV, and import those CSVs into the DB."""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
from django.conf import settings

from stocks.models import HistoricStock, Instrument
from stocks.services import smartapi

logger = logging.getLogger(__name__)

SUPPORTED_EXCHANGES = ("BSE", "NSE")
CANDLE_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
FETCHED_CSV_PREFIX = "smart_fetched_"
IMPORT_BATCH_SIZE = 5000


class RateLimitExceeded(RuntimeError):
    """Angel One rejected a request for exceeding its access rate."""


RATE_LIMIT_MARKER = "exceeding access rate"


def _signal_stop(stop_event):
    # Set from the worker itself so the other threads stop before their next request.
    if stop_event is not None:
        stop_event.set()


def fetch_candles(client, instrument, start_date, end_date, stop_event=None):
    """Fetch daily candles for one instrument; returns a DataFrame or None.

    Raises RateLimitExceeded when Angel One reports the access rate was exceeded.
    """
    if stop_event is not None and stop_event.is_set():
        return None

    params = {
        "exchange": instrument.exch_seg,
        "symboltoken": str(int(instrument.token)),
        "interval": "ONE_DAY",
        "fromdate": start_date,
        "todate": end_date,
    }
    try:
        response = client.getCandleData(params)
        time.sleep(1)  # stay under SmartAPI's rate limit
    except Exception as exc:
        if RATE_LIMIT_MARKER in str(exc).lower():
            _signal_stop(stop_event)
            raise RateLimitExceeded(str(exc)) from exc
        logger.exception("Error fetching token %s", instrument.token)
        return None

    if response and RATE_LIMIT_MARKER in str(response.get("message", "")).lower():
        _signal_stop(stop_event)
        raise RateLimitExceeded(response["message"])
    if not response or not response.get("status") or not response.get("data"):
        return None

    df = pd.DataFrame(response["data"], columns=CANDLE_COLUMNS)
    df["symboltoken"] = instrument.token
    df["exchange"] = instrument.exch_seg
    df["symbol"] = instrument.symbol
    return df


def fetch_historic_to_csv(start_date, end_date, *, on_progress=None, cancel_event=None):
    """Fetch candles for every BSE/NSE instrument and write them to a CSV in DATA_DIR.

    `start_date` / `end_date` use SmartAPI's "YYYY-MM-DD HH:MM" format. If Angel One
    reports the access rate was exceeded, the run stops: pending requests are cancelled
    and whatever was fetched so far is still saved.

    `on_progress(counts)` is called with {"total", "fetched", "failed", "rows"} as
    instruments complete. Setting `cancel_event` from outside stops the run the same way.
    """
    client = smartapi.get_client()
    instruments = list(Instrument.objects.filter(exch_seg__in=SUPPORTED_EXCHANGES))
    logger.info("Fetching candles for %d instruments", len(instruments))

    results = []
    failed = []
    rows_so_far = 0
    stopped_reason = None
    stop_event = threading.Event()  # tells workers to skip their remaining requests

    def report():
        if on_progress:
            on_progress({"total": len(instruments), "fetched": len(results),
                         "failed": len(failed), "rows": rows_so_far})

    report()
    with ThreadPoolExecutor(settings.HISTORIC_FETCH_MAX_THREADS) as executor:
        futures = {
            executor.submit(fetch_candles, client, inst, start_date, end_date, stop_event): inst
            for inst in instruments
        }
        for future in as_completed(futures):
            inst = futures[future]
            try:
                df = future.result()
            except RateLimitExceeded as exc:
                stopped_reason = f"Angel One rate limit exceeded: {exc}"
                logger.error("Stopping fetch at token %s: %s", inst.token, stopped_reason)
                stop_event.set()
                executor.shutdown(wait=False, cancel_futures=True)
                break
            if df is None and stop_event.is_set():
                continue  # skipped because the run is stopping, not a real failure
            if df is None or df.empty:
                failed.append({"symbol": inst.symbol, "token": inst.token, "exchange": inst.exch_seg})
            else:
                results.append(df)
                rows_so_far += len(df)
            report()
            if cancel_event is not None and cancel_event.is_set():
                stopped_reason = "Cancelled by user"
                logger.info("Fetch cancelled")
                stop_event.set()
                executor.shutdown(wait=False, cancel_futures=True)
                break

    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    fetched_csv = None
    rows = 0
    if results:
        final_df = pd.concat(results, ignore_index=True)
        rows = len(final_df)
        fetched_csv = settings.DATA_DIR / f"{FETCHED_CSV_PREFIX}{stamp}.csv"
        final_df.to_csv(fetched_csv, index=False)

    failed_csv = None
    if failed:
        failed_csv = settings.DATA_DIR / f"failed_instruments_{stamp}.csv"
        pd.DataFrame(failed).to_csv(failed_csv, index=False)

    return {
        "stopped": stopped_reason is not None,
        "cancelled": stopped_reason == "Cancelled by user",
        "stopped_reason": stopped_reason,
        "instruments": len(instruments),
        "fetched": len(results),
        "failed": len(failed),
        "not_attempted": len(instruments) - len(results) - len(failed),
        "rows": rows,
        "csv_file": fetched_csv.name if fetched_csv else None,
        "failed_csv_file": failed_csv.name if failed_csv else None,
    }


def resolve_csv(file_name=None):
    """Return a CSV path inside DATA_DIR; defaults to the newest fetched CSV."""
    data_dir = settings.DATA_DIR.resolve()
    if file_name:
        path = (data_dir / file_name).resolve()
        if path.parent != data_dir:
            raise ValueError("csv_file must be a file name inside DATA_DIR")
    else:
        candidates = sorted(data_dir.glob(f"{FETCHED_CSV_PREFIX}*.csv"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            raise FileNotFoundError(f"No {FETCHED_CSV_PREFIX}*.csv files in {data_dir}")
        path = candidates[-1]
    if not path.is_file():
        raise FileNotFoundError(f"{path.name} not found in {data_dir}")
    return path


def import_historic_csv(csv_path: Path):
    """Bulk insert a fetched CSV into historicstocks. Returns rows inserted."""
    df = pd.read_csv(csv_path, dtype={"symboltoken": str})
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.astype(object).where(df.notna(), None)

    objects = [HistoricStock(**record) for record in df.to_dict(orient="records")]
    HistoricStock.objects.bulk_create(objects, batch_size=IMPORT_BATCH_SIZE)
    logger.info("Inserted %d rows from %s", len(objects), csv_path.name)
    return len(objects)
