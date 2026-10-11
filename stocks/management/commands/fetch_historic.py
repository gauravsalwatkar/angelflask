from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from stocks.serializers import SMARTAPI_DATETIME_FORMAT
from stocks.services.historic import fetch_historic_to_csv, import_historic_csv, resolve_csv


def smartapi_datetime(value):
    try:
        datetime.strptime(value, SMARTAPI_DATETIME_FORMAT)
    except ValueError:
        raise CommandError(f"'{value}' is not in 'YYYY-MM-DD HH:MM' format")
    return value


class Command(BaseCommand):
    help = "Fetch daily candles for all BSE/NSE instruments into a CSV in DATA_DIR."

    def add_arguments(self, parser):
        parser.add_argument("start_date", type=smartapi_datetime, help='e.g. "2026-05-23 09:15"')
        parser.add_argument("end_date", type=smartapi_datetime, help='e.g. "2026-07-04 15:30"')
        parser.add_argument("--import", dest="import_csv", action="store_true",
                            help="Insert the fetched CSV into historicstocks afterwards")

    def handle(self, *args, start_date, end_date, import_csv, **options):
        def on_progress(p):
            done = p["fetched"] + p["failed"]
            if done % 50 == 0 or done == p["total"]:
                self.stdout.write(
                    f"{done}/{p['total']} instruments ({p['fetched']} fetched, {p['failed']} failed, "
                    f"{p['rows']} candles)"
                )

        summary = fetch_historic_to_csv(start_date, end_date, on_progress=on_progress)
        self.stdout.write(str(summary))

        if summary["stopped"]:
            raise CommandError(
                f"Fetch stopped: {summary['stopped_reason']}. Partial data saved to "
                f"{summary['csv_file']}; {summary['not_attempted']} instruments not fetched. "
                "Skipping --import."
            )

        if import_csv and summary["csv_file"]:
            rows = import_historic_csv(resolve_csv(summary["csv_file"]))
            self.stdout.write(self.style.SUCCESS(f"Inserted {rows} rows"))
