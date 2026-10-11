from rest_framework import serializers

from stocks.models import TrackedSignal
from stocks.services import tracking

SMARTAPI_DATETIME_FORMAT = "%Y-%m-%d %H:%M"


class FetchHistoricSerializer(serializers.Serializer):
    """Body for /fetch-smart-historic, e.g. {"start_date": "2026-05-23 09:15", "end_date": "2026-07-04 15:30"}."""

    start_date = serializers.DateTimeField(input_formats=[SMARTAPI_DATETIME_FORMAT])
    end_date = serializers.DateTimeField(input_formats=[SMARTAPI_DATETIME_FORMAT])

    def validate(self, attrs):
        if attrs["start_date"] > attrs["end_date"]:
            raise serializers.ValidationError("start_date must be before end_date")
        return attrs


class InsertDataSerializer(serializers.Serializer):
    csv_file = serializers.CharField(
        required=False,
        help_text="CSV file name inside DATA_DIR; defaults to the newest smart_fetched_*.csv",
    )


class SupportZoneQuerySerializer(serializers.Serializer):
    limit = serializers.IntegerField(default=2, min_value=1, max_value=5)
    swing_order = serializers.IntegerField(default=5, min_value=1)
    eps_pct = serializers.FloatField(default=0.003, min_value=0)

    min_touches = serializers.IntegerField(default=3)
    min_strength_score = serializers.FloatField(default=12.0)
    min_bounce_pct = serializers.FloatField(default=1.5)
    min_volume_factor = serializers.FloatField(default=1.2)
    max_distance_pct = serializers.FloatField(default=2.0)


class SwingZoneQuerySerializer(SupportZoneQuerySerializer):
    eps_pct = serializers.FloatField(default=0.005, min_value=0)

    min_touches = serializers.IntegerField(default=2)
    min_strength_score = serializers.FloatField(default=8.0)
    min_bounce_pct = serializers.FloatField(default=2.0)
    min_volume_factor = serializers.FloatField(default=1.5)
    max_distance_pct = serializers.FloatField(default=5.0)

    rsi_period = serializers.IntegerField(default=14, min_value=1)
    rsi_oversold = serializers.FloatField(default=40.0)
    min_trend_score = serializers.FloatField(default=0.3)


class BullishEngulfingQuerySerializer(SupportZoneQuerySerializer):
    eps_pct = serializers.FloatField(default=0.005, min_value=0)

    min_touches = serializers.IntegerField(default=2)
    min_strength_score = serializers.FloatField(default=8.0)
    min_bounce_pct = serializers.FloatField(default=2.0)
    min_volume_factor = serializers.FloatField(default=2.0)
    max_distance_pct = serializers.FloatField(default=4.0)

    min_engulfing_strength = serializers.FloatField(default=0.7)
    rsi_period = serializers.IntegerField(default=14, min_value=1)
    rsi_oversold = serializers.FloatField(default=45.0)


class SignalQuerySerializer(serializers.Serializer):
    as_of = serializers.DateField(
        required=False,
        help_text="Predict as of this date, using only candles up to it (default: latest candle)",
    )
    limit = serializers.IntegerField(default=25, min_value=1, max_value=500)
    min_price = serializers.FloatField(default=20.0, min_value=0)
    min_avg_turnover = serializers.FloatField(
        default=5_000_000, min_value=0,
        help_text="Minimum 20-day average daily turnover (close x volume) in rupees",
    )


class TopPicksQuerySerializer(SignalQuerySerializer):
    min_signals = serializers.IntegerField(default=2, min_value=1, max_value=10)


def _pct(value, base):
    return round((value / base - 1) * 100, 2) if value is not None and base else None


class TrackedSignalSerializer(serializers.ModelSerializer):
    """A saved buy signal. Send symbol, buy_price and target; the rest is optional.

    exchange/symboltoken are looked up from the stored candles when missing, and
    signal_date defaults to the symbol's latest candle date.
    """

    signal_date = serializers.DateField(required=False)
    target_pct = serializers.SerializerMethodField()
    stop_loss_pct = serializers.SerializerMethodField()
    max_gain_pct = serializers.SerializerMethodField()
    current_return_pct = serializers.SerializerMethodField()

    class Meta:
        model = TrackedSignal
        fields = [
            "id", "symbol", "exchange", "symboltoken", "technique", "signal_date",
            "buy_price", "target", "stop_loss", "target_pct", "stop_loss_pct",
            "status", "hit_date", "trading_days", "calendar_days",
            "max_high", "max_gain_pct", "min_low", "last_close", "current_return_pct",
            "checked_through", "verified_at", "notes", "created_at",
        ]
        read_only_fields = [
            "status", "hit_date", "trading_days", "calendar_days", "max_high", "min_low",
            "last_close", "checked_through", "verified_at", "created_at",
        ]

    def get_target_pct(self, obj):
        return _pct(obj.target, obj.buy_price)

    def get_stop_loss_pct(self, obj):
        return _pct(obj.stop_loss, obj.buy_price)

    def get_max_gain_pct(self, obj):
        return _pct(obj.max_high, obj.buy_price)

    def get_current_return_pct(self, obj):
        return _pct(obj.last_close, obj.buy_price)

    def validate(self, attrs):
        buy, target, stop = attrs["buy_price"], attrs["target"], attrs.get("stop_loss")
        if buy <= 0:
            raise serializers.ValidationError({"buy_price": "Must be greater than 0"})
        if target <= buy:
            raise serializers.ValidationError({"target": "Must be above the buy price"})
        if stop is not None and stop >= buy:
            raise serializers.ValidationError({"stop_loss": "Must be below the buy price"})

        if not attrs.get("symboltoken"):
            try:
                symbol, exchange, token, latest = tracking.resolve_instrument(
                    attrs["symbol"].strip(), attrs.get("exchange", "").strip()
                )
            except tracking.UnknownSymbol as exc:
                raise serializers.ValidationError({"symbol": str(exc)})
            attrs.update(symbol=symbol, exchange=exchange, symboltoken=token)
            attrs.setdefault("signal_date", latest)
        if not attrs.get("signal_date"):
            raise serializers.ValidationError({"signal_date": "This field is required"})

        duplicate = TrackedSignal.objects.filter(
            symboltoken=attrs["symboltoken"], exchange=attrs.get("exchange", ""),
            signal_date=attrs["signal_date"], buy_price=buy, target=target,
        ).first()
        if duplicate:
            raise serializers.ValidationError(f"Already tracked as #{duplicate.id}")
        return attrs


class VerifySignalsSerializer(serializers.Serializer):
    """Body for /tracked-signals/verify; with no filters every tracked signal is re-checked."""

    signal_date = serializers.DateField(required=False, help_text="Only signals from this candle date")
    from_date = serializers.DateField(required=False)
    to_date = serializers.DateField(required=False)
    technique = serializers.CharField(required=False)
    ids = serializers.ListField(child=serializers.IntegerField(), required=False)

    def validate(self, attrs):
        if attrs.get("from_date") and attrs.get("to_date") and attrs["from_date"] > attrs["to_date"]:
            raise serializers.ValidationError("from_date must be on or before to_date")
        return attrs

    def filter(self, queryset):
        d = self.validated_data
        if d.get("signal_date"):
            queryset = queryset.filter(signal_date=d["signal_date"])
        if d.get("from_date"):
            queryset = queryset.filter(signal_date__gte=d["from_date"])
        if d.get("to_date"):
            queryset = queryset.filter(signal_date__lte=d["to_date"])
        if d.get("technique"):
            queryset = queryset.filter(technique=d["technique"].replace("-", "_"))
        if d.get("ids"):
            queryset = queryset.filter(pk__in=d["ids"])
        return queryset


class NewsScanSerializer(serializers.Serializer):
    """Query for /news/scan: scan the given symbols, or the current picks of a technique."""

    technique = serializers.CharField(default="top-picks")
    limit = serializers.IntegerField(default=15, min_value=1, max_value=30)
    symbols = serializers.CharField(required=False, allow_blank=True,
                                    help_text="Comma-separated symbols, e.g. TCS,INFY (overrides technique)")
    min_price = serializers.FloatField(default=20.0, min_value=0)
    min_avg_turnover = serializers.FloatField(default=5_000_000, min_value=0)
    refresh = serializers.BooleanField(default=False, help_text="Ignore the cached result and fetch news again")

    def validate_symbols(self, value):
        symbols = [s.strip().upper() for s in value.split(",") if s.strip()]
        if len(symbols) > 30:
            raise serializers.ValidationError("At most 30 symbols per scan")
        return symbols


class RecommendationSerializer(serializers.Serializer):
    count = serializers.IntegerField(default=3, min_value=1, max_value=20)
    min_price = serializers.FloatField(default=20.0, min_value=0)
    min_avg_turnover = serializers.FloatField(default=5_000_000, min_value=0)
    news = serializers.BooleanField(default=False, help_text="Also check news with Claude and drop stocks with bad news")
    as_of = serializers.DateField(required=False,
                                  help_text="Recommend as of this past date, using only candles up to it")

    def validate(self, attrs):
        if attrs.get("news") and attrs.get("as_of"):
            raise serializers.ValidationError("News is only available for today, not for a past as_of date")
        return attrs
