from django.core.management.base import BaseCommand, CommandError

from stocks.services.historic import import_historic_csv, resolve_csv


class Command(BaseCommand):
    help = "Bulk insert a fetched candles CSV from DATA_DIR into historicstocks."

    def add_arguments(self, parser):
        parser.add_argument("csv_file", nargs="?",
                            help="File name inside DATA_DIR (default: newest smart_fetched_*.csv)")

    def handle(self, *args, csv_file, **options):
        try:
            path = resolve_csv(csv_file)
        except (ValueError, FileNotFoundError) as exc:
            raise CommandError(exc)
        rows = import_historic_csv(path)
        self.stdout.write(self.style.SUCCESS(f"Inserted {rows} rows from {path.name}"))
