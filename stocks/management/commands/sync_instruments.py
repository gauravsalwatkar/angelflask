from django.core.management.base import BaseCommand

from stocks.services.instruments import sync_instruments


class Command(BaseCommand):
    help = "Download the Angel One scrip master and store new instruments."

    def handle(self, *args, **options):
        count = sync_instruments()
        self.stdout.write(self.style.SUCCESS(f"Inserted {count} new instruments"))
