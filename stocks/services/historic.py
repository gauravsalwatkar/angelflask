"""Fetch daily candles from SmartAPI to CSV, and import those CSVs into the DB."""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
from django.conf import settings
from django.db.models import Q

from stocks.models import HistoricStock, Instrument
from stocks.services import smartapi

logger = logging.getLogger(__name__)

# NSE cash-segment series that are listed shares; the rest (-SG, -N0, -GS, -TB, -MF, ...) are
# bonds, G-secs, T-bills and the like.
NSE_STOCK_SERIES = ("EQ", "BE", "SM", "ST", "BZ")
CANDLE_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
FETCHED_CSV_PREFIX = "smart_fetched_"
IMPORT_BATCH_SIZE = 5000


class RateLimitExceeded(RuntimeError):
    """Angel One rejected a request for exceeding its access rate."""


RATE_LIMIT_MARKER = "exceeding access rate"
# Seconds to wait before each retry of a rate-limited request; the run stops once these run out.
RATE_LIMIT_BACKOFF = (2, 5, 15, 30, 60, 120, 300)


def stock_instruments():
    """BSE/NSE equity shares only - no indices (AMXIDX), bonds, G-secs, T-bills or fund units."""
    nse = Q()
    for series in NSE_STOCK_SERIES:
        nse |= Q(symbol__endswith=f"-{series}")
    # BSE equity scrip codes are 5xxxxx; 7/8/9xxxxx are debt instruments and commercial paper.
    # Some debt also sits in 5xxxxx with symbols like 07GPG / 11MPD (two digits + three letters).
    bse = Q(exch_seg="BSE", token__startswith="5") & ~Q(symbol__regex=r"^[0-9]{2}[A-Z]{3}$")
    return Instrument.objects.filter(Q(instrumenttype="") | Q(instrumenttype__isnull=True)).filter(
        (Q(exch_seg="NSE") & nse) | bse
    )


def missing_tokens(since, until=None):
    """Tokens of stock instruments with no stored candle between `since` and `until` (dates, inclusive)."""
    have = HistoricStock.objects.filter(date__gte=since)
    if until:
        have = have.filter(date__lte=until)
    # Collect the tokens in Python: a NOT IN (subquery) over historicstocks can make Postgres rescan
    # the whole table once per instrument, which takes minutes.
    have = set(have.values_list("symboltoken", flat=True).distinct())
    return [t for t in stock_instruments().values_list("token", flat=True) if t not in have]


def _signal_stop(stop_event):
    # Set from the worker itself so the other threads stop before their next request.
    if stop_event is not None:
        stop_event.set()


def fetch_candles(client, instrument, start_date, end_date, stop_event=None, interval="ONE_DAY"):
    """Fetch candles (daily unless `interval` says otherwise) for one instrument; returns a DataFrame or None.

    Raises RateLimitExceeded when Angel One still reports the access rate was exceeded
    after every RATE_LIMIT_BACKOFF retry.
    """
    if stop_event is not None and stop_event.is_set():
        return None

    params = {
        "exchange": instrument.exch_seg,
        "symboltoken": str(int(instrument.token)),
        "interval": interval,
        "fromdate": start_date,
        "todate": end_date,
    }
    for attempt, wait in enumerate((*RATE_LIMIT_BACKOFF, None)):
        try:
            response = client.getCandleData(params)
            time.sleep(1)  # stay under SmartAPI's rate limit
            rate_limited = response and RATE_LIMIT_MARKER in str(response.get("message", "")).lower()
            error = response.get("message") if rate_limited else None
        except Exception as exc:
            if RATE_LIMIT_MARKER not in str(exc).lower():
                logger.exception("Error fetching token %s", instrument.token)
                return None
            rate_limited, error = True, str(exc)

        if not rate_limited:
            break
        if wait is None:
            _signal_stop(stop_event)
            raise RateLimitExceeded(error)
        logger.warning("Rate limited on token %s, retry %d in %ds", instrument.token, attempt + 1, wait)
        # Sleep on the stop event so a cancel or another thread's give-up ends the wait early.
        if stop_event is None:
            time.sleep(wait)
        elif stop_event.wait(wait):
            return None

    if not response or not response.get("status") or not response.get("data"):
        if response and not response.get("status"):
            logger.warning("No candles for token %s: %s", instrument.token, response.get("message"))
        return None

    df = pd.DataFrame(response["data"], columns=CANDLE_COLUMNS)
    df["symboltoken"] = instrument.token
    df["exchange"] = instrument.exch_seg
    df["symbol"] = instrument.symbol
    return df


def fetch_historic_to_csv(start_date, end_date, *, tokens=None, on_progress=None, cancel_event=None):
    """Fetch candles for every BSE/NSE stock and write them to a CSV in DATA_DIR.

    Pass `tokens` to fetch exactly those instruments instead of all stocks.

    `start_date` / `end_date` use SmartAPI's "YYYY-MM-DD HH:MM" format. If Angel One
    reports the access rate was exceeded, the request is retried with backoff; if it is still
    refused, the run stops: pending requests are cancelled and whatever was fetched so far
    is still saved.

    `on_progress(counts)` is called with {"total", "fetched", "failed", "rows"} as
    instruments complete. Setting `cancel_event` from outside stops the run the same way.
    """
    # Daily candles are stamped 00:00, so a from-time like 09:15 would skip the first day's candle.
    start_date = f"{start_date[:10]} 00:00"
    client = smartapi.get_client()
    if tokens is None:
        instruments = list(stock_instruments())
    else:
        instruments = list(Instrument.objects.filter(token__in=tokens))
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
    """Bulk insert a fetched CSV into historicstocks, skipping candles already stored.

    Returns rows inserted.
    """
    df = pd.read_csv(csv_path, dtype={"symboltoken": str})
    df["date"] = pd.to_datetime(df["date"]).dt.date

    existing = set(
        HistoricStock.objects.filter(symboltoken__in=df["symboltoken"].unique().tolist())
        .values_list("symboltoken", "date")
    )
    df = df.drop_duplicates(["symboltoken", "date"])
    df = df[[key not in existing for key in zip(df["symboltoken"], df["date"])]]
    df = df.astype(object).where(df.notna(), None)

    objects = [HistoricStock(**record) for record in df.to_dict(orient="records")]
    HistoricStock.objects.bulk_create(objects, batch_size=IMPORT_BATCH_SIZE)
    logger.info("Inserted %d rows from %s", len(objects), csv_path.name)
    return len(objects)
