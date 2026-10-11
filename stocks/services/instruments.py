import logging

import requests
from django.conf import settings

from stocks.models import Instrument
from stocks.utils import clean_date, clean_decimal

logger = logging.getLogger(__name__)

BATCH_SIZE = 500


def sync_instruments():
    """Download the Angel One scrip master and insert instruments not yet stored.

    Returns the number of newly inserted instruments.
    """
    response = requests.get(settings.ANGEL_ONE["SCRIP_MASTER_URL"], timeout=120)
    response.raise_for_status()
    stock_data = response.json()

    seen_tokens = set(Instrument.objects.values_list("token", flat=True))
    new_instruments = []

    for item in stock_data:
        token = str(item.get("token"))
        if token in seen_tokens:
            continue
        seen_tokens.add(token)

        new_instruments.append(
            Instrument(
                token=token,
                symbol=item.get("symbol"),
                name=item.get("name"),
                expiry=clean_date(item.get("expiry")),
                strike=clean_decimal(item.get("strike")),
                lotsize=int(item.get("lotsize", 0) or 0),
                instrumenttype=item.get("instrumenttype"),
                exch_seg=item.get("exch_seg"),
                tick_size=clean_decimal(item.get("tick_size")),
            )
        )

    Instrument.objects.bulk_create(new_instruments, batch_size=BATCH_SIZE)
    logger.info("Inserted %d new instruments", len(new_instruments))
    return len(new_instruments)
