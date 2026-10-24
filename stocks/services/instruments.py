import logging
from datetime import date

import requests
from django.conf import settings

from stocks.models import Instrument
from stocks.utils import clean_date, clean_decimal

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
# Underlyings whose index options the option-levels calculator needs.
INDEX_OPTION_NAMES = ("NIFTY", "BANKNIFTY")


def _download_scrip_master():
    response = requests.get(settings.ANGEL_ONE["SCRIP_MASTER_URL"], timeout=120)
    response.raise_for_status()
    return response.json()


def _to_instrument(item):
    return Instrument(
        token=str(item.get("token")),
        symbol=item.get("symbol"),
        name=item.get("name"),
        expiry=clean_date(item.get("expiry")),
        strike=clean_decimal(item.get("strike")),
        lotsize=int(item.get("lotsize", 0) or 0),
        instrumenttype=item.get("instrumenttype"),
        exch_seg=item.get("exch_seg"),
        tick_size=clean_decimal(item.get("tick_size")),
    )


def _insert_new(items):
    """Insert scrip master items whose token is not stored yet; returns the number inserted."""
    seen_tokens = set(Instrument.objects.values_list("token", flat=True))
    new_instruments = []
    for item in items:
        token = str(item.get("token"))
        if token in seen_tokens:
            continue
        seen_tokens.add(token)
        new_instruments.append(_to_instrument(item))

    Instrument.objects.bulk_create(new_instruments, batch_size=BATCH_SIZE)
    return len(new_instruments)


def sync_instruments():
    """Download the Angel One scrip master and insert instruments not yet stored.

    Returns the number of newly inserted instruments.
    """
    count = _insert_new(_download_scrip_master())
    logger.info("Inserted %d new instruments", count)
    return count


def is_index_option_item(item):
    return (item.get("exch_seg") == "NFO" and item.get("instrumenttype") == "OPTIDX"
            and item.get("name") in INDEX_OPTION_NAMES)


def is_index_item(item):
    return (item.get("exch_seg") == "NSE" and item.get("instrumenttype") == "AMXIDX"
            and item.get("name") in INDEX_OPTION_NAMES)


def sync_index_options():
    """Store the NIFTY / BANKNIFTY indices and their live option contracts; drop expired ones.

    Unlike sync_instruments, this leaves the rest of the scrip master out of `instruments`.
    """
    items = [i for i in _download_scrip_master() if is_index_option_item(i) or is_index_item(i)]
    expired, _ = Instrument.objects.filter(
        exch_seg="NFO", instrumenttype="OPTIDX", name__in=INDEX_OPTION_NAMES, expiry__lt=date.today()
    ).delete()
    inserted = _insert_new(items)
    logger.info("Index options: inserted %d, removed %d expired", inserted, expired)
    return {"inserted": inserted, "expired_removed": expired}
