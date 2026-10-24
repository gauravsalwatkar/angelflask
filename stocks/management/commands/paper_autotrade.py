import logging

from django.core.management.base import BaseCommand

from stocks.services import autotrade


class Command(BaseCommand):
    help = ("Automatic paper trading: picks stocks with the app's rules and paper-trades them in the intraday, "
            "short-term and long-term books, every --interval seconds until Ctrl+C. No real orders.")

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=autotrade.CYCLE_SECONDS, help="Seconds between cycles")
        parser.add_argument("--once", action="store_true", help="Run one cycle and exit")

    def handle(self, *args, interval, once, **options):
        # Show the trader's own log lines (buys, exits, skips) in the terminal.
        handler = logging.StreamHandler(self.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        log = logging.getLogger(autotrade.__name__)
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
        try:
            autotrade.run(interval, once=once)
        except KeyboardInterrupt:
            autotrade.stop()
            self.stdout.write(self.style.SUCCESS("Stopped."))
