from django.core.management.base import BaseCommand

from stocks.analysis import backtest


class Command(BaseCommand):
    help = "Backtest every buy-signal technique on the stored candles and save the results to DATA_DIR/backtest.json."

    def add_arguments(self, parser):
        parser.add_argument("--step", type=int, default=backtest.STEP, help="Trading days between signal dates")
        parser.add_argument("--hold", type=int, default=backtest.HOLD, help="Maximum holding period in sessions")

    def handle(self, *args, step, hold, **options):
        def progress(n, total, day, trades):
            if n % 10 == 0 or n == total:
                self.stdout.write(f"{n}/{total} dates (up to {day}), {trades:,} trades")

        summary = backtest.run_and_save(step, hold, progress)
        self.stdout.write(f"\nPeriod {summary['period']['from']} .. {summary['period']['to']}")
        self.stdout.write(f"{'technique':20} {'trades':>7} {'avg %':>7} {'vs all':>7} {'win %':>6} {'PF':>5}")
        rows = sorted(summary["techniques"].items(), key=lambda kv: -kv[1]["excess_return_pct"])
        for name, s in [("ALL STOCKS", summary["market"]), *rows]:
            self.stdout.write(f"{name:20} {s['trades']:>7} {s['avg_return_pct']:>7} {s['excess_return_pct']:>7} "
                              f"{s['win_rate_pct']:>6} {s['profit_factor'] or '-':>5}")
        self.stdout.write(self.style.SUCCESS(f"Saved {backtest.results_path()}"))
