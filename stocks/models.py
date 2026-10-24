from django.db import models


class Instrument(models.Model):
    """Angel One scrip master entry."""

    token = models.CharField(max_length=50, primary_key=True)
    symbol = models.CharField(max_length=50)
    name = models.CharField(max_length=100, null=True, blank=True)
    expiry = models.DateField(null=True, blank=True)
    strike = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    lotsize = models.IntegerField(null=True, blank=True)
    instrumenttype = models.CharField(max_length=20, null=True, blank=True)
    exch_seg = models.CharField(max_length=20, null=True, blank=True)
    tick_size = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)

    class Meta:
        # Tables already exist from the FastAPI version; Django must not alter them.
        managed = False
        db_table = "instruments"

    def __str__(self):
        return f"{self.symbol} ({self.exch_seg})"


class HistoricStock(models.Model):
    """Daily OHLCV candle for one instrument."""

    # The table has one row per (symboltoken, date); Django needs a single-column
    # primary key, so this is only safe for inserts and read queries, not .save().
    symboltoken = models.CharField(max_length=2000, primary_key=True)
    date = models.DateField(null=True, blank=True)
    open = models.FloatField(null=True, blank=True)
    close = models.FloatField(null=True, blank=True)
    high = models.FloatField(null=True, blank=True)
    low = models.FloatField(null=True, blank=True)
    volume = models.FloatField(null=True, blank=True)
    exchange = models.CharField(max_length=1000, null=True, blank=True)
    symbol = models.CharField(max_length=200, null=True, blank=True)

    class Meta:
        managed = False
        db_table = "historicstocks"

    def __str__(self):
        return f"{self.symbol} {self.date}"


class TrackedSignal(models.Model):
    """A buy signal saved for follow-up: did price reach the target, and how fast?"""

    OPEN = "open"
    TARGET_HIT = "target_hit"
    STOP_HIT = "stop_hit"
    STATUS_CHOICES = [(OPEN, "Open"), (TARGET_HIT, "Target hit"), (STOP_HIT, "Stop loss hit")]

    symbol = models.CharField(max_length=200)
    exchange = models.CharField(max_length=20, blank=True, default="")
    symboltoken = models.CharField(max_length=50, blank=True, default="")
    technique = models.CharField(max_length=50, blank=True, default="manual")
    signal_date = models.DateField(help_text="Candle date of the signal; only later candles are checked")
    buy_price = models.FloatField()
    target = models.FloatField()
    stop_loss = models.FloatField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    # Filled in by verification
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=OPEN)
    hit_date = models.DateField(null=True, blank=True)
    trading_days = models.IntegerField(null=True, blank=True, help_text="Candles from signal to hit (or checked so far)")
    calendar_days = models.IntegerField(null=True, blank=True)
    max_high = models.FloatField(null=True, blank=True)
    min_low = models.FloatField(null=True, blank=True)
    last_close = models.FloatField(null=True, blank=True)
    checked_through = models.DateField(null=True, blank=True, help_text="Latest candle date verified")
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "tracked_signals"
        ordering = ["-signal_date", "-id"]
        indexes = [models.Index(fields=["status"]), models.Index(fields=["symbol"])]

    def __str__(self):
        return f"{self.symbol} @ {self.buy_price} -> {self.target} ({self.status})"


class PaperTrade(models.Model):
    """A simulated trade in one of three paper books; filled at the live price, no real order is placed."""

    INTRADAY = "intraday"
    SHORT_TERM = "short_term"
    LONG_TERM = "long_term"
    BOOK_CHOICES = [(INTRADAY, "Intraday"), (SHORT_TERM, "Short-term"), (LONG_TERM, "Long-term")]
    BUY, SELL = "BUY", "SELL"
    SIDE_CHOICES = [(BUY, "Buy"), (SELL, "Sell short")]
    OPEN, CLOSED = "open", "closed"
    STATUS_CHOICES = [(OPEN, "Open"), (CLOSED, "Closed")]

    book = models.CharField(max_length=20, choices=BOOK_CHOICES)
    symbol = models.CharField(max_length=200)
    exchange = models.CharField(max_length=20)
    token = models.CharField(max_length=50)
    side = models.CharField(max_length=4, choices=SIDE_CHOICES, default=BUY)
    qty = models.IntegerField()
    entry_price = models.FloatField()
    entry_at = models.DateTimeField()
    stop_loss = models.FloatField(null=True, blank=True)
    target = models.FloatField(null=True, blank=True)
    source = models.CharField(max_length=50, blank=True, default="manual", help_text="What suggested the trade")
    technique = models.CharField(max_length=200, blank=True, default="manual",
                                 help_text="Technique(s) behind the trade, '+'-joined, e.g. momentum_leaders+macd_bullish")
    auto = models.BooleanField(default=False, help_text="Placed by the automatic paper trader")
    signal_date = models.DateField(null=True, blank=True, help_text="Candle date of the plan the trade came from")
    signal = models.JSONField(default=dict, blank=True, help_text="Why it was bought: the signal's numbers and reasons")
    notes = models.TextField(blank=True, default="")

    # Tracked while open (and kept after the exit), for learning which rules work
    target_hit_at = models.DateTimeField(null=True, blank=True, help_text="When the target was first reached")
    best_price = models.FloatField(null=True, blank=True, help_text="Most favourable price seen while open")
    worst_price = models.FloatField(null=True, blank=True, help_text="Most adverse price seen while open")

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=OPEN)
    exit_price = models.FloatField(null=True, blank=True)
    exit_at = models.DateTimeField(null=True, blank=True)
    exit_reason = models.CharField(max_length=20, blank=True, default="", help_text="target, stop, square_off or manual")
    costs = models.FloatField(default=0, help_text="Brokerage and taxes, rupees, deducted at exit")
    pnl = models.FloatField(null=True, blank=True, help_text="Net profit or loss after costs, rupees")

    class Meta:
        db_table = "paper_trades"
        ordering = ["-entry_at", "-id"]
        indexes = [models.Index(fields=["book", "status"]), models.Index(fields=["technique"])]

    @property
    def direction(self):
        return 1 if self.side == self.BUY else -1

    def __str__(self):
        return f"{self.book} {self.side} {self.qty} {self.symbol} @ {self.entry_price} ({self.status})"
