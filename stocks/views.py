import logging
from datetime import datetime

from django.conf import settings
from django.db import connection
from django.db.models import Max
from django.shortcuts import render
from django.urls import reverse
from rest_framework import fields as drf_fields
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from stocks import serializers
from stocks.analysis import outcomes, recommend, screeners, signals
from stocks.models import HistoricStock, Instrument, TrackedSignal
from stocks.services import fetch_jobs, historic, instruments, news, smartapi, tracking
from stocks.services.smartapi import SmartApiLoginError

logger = logging.getLogger(__name__)

DASHBOARD_SCREENERS = [
    {
        "id": "strong",
        "title": "Strong support zones",
        "description": "Support zones near the current price, from 1 year of candles.",
        "url_name": "strong-support-zones",
        "serializer": serializers.SupportZoneQuerySerializer,
    },
    {
        "id": "swing",
        "title": "Swing zones",
        "description": "Support zones confirmed by RSI and trend, from 6 months of high-volume symbols.",
        "url_name": "swing-zones",
        "serializer": serializers.SwingZoneQuerySerializer,
    },
    {
        "id": "engulfing-support",
        "title": "Engulfing on support",
        "description": "Bullish engulfing on the last candle, sitting on a strong support zone.",
        "url_name": "support-bullish-engulfing",
        "serializer": serializers.BullishEngulfingQuerySerializer,
    },
]


def _form_fields(serializer_class):
    """Describe a query serializer's fields so the template can render inputs."""
    result = []
    for name, field in serializer_class().fields.items():
        is_int = isinstance(field, drf_fields.IntegerField)
        is_date = isinstance(field, drf_fields.DateField)
        result.append({
            "name": name,
            "label": "predict as of" if name == "as_of" else name.replace("_", " "),
            "type": "date" if is_date else "number",
            "default": "" if field.default is drf_fields.empty else field.default,
            "hint": field.help_text or "",
            "step": None if is_date else "1" if is_int else "any",
            "min": getattr(field, "min_value", None),
            "max": getattr(field, "max_value", None),
        })
    return result


def _dashboard_stats():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT reltuples::bigint FROM pg_class WHERE relname = %s",
            [HistoricStock._meta.db_table],
        )
        row = cursor.fetchone()
    return {
        "instruments": Instrument.objects.count(),
        "candles_estimate": max(row[0], 0) if row else 0,
        "latest_candle": HistoricStock.objects.order_by().aggregate(latest=Max("date"))["latest"],
        "fetchable": Instrument.objects.filter(exch_seg__in=historic.SUPPORTED_EXCHANGES).count(),
    }


def _csv_files():
    data_dir = settings.DATA_DIR
    if not data_dir.is_dir():
        return []
    files = sorted(
        data_dir.glob(f"{historic.FETCHED_CSV_PREFIX}*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return [
        {
            "name": p.name,
            "size_mb": round(p.stat().st_size / 1_000_000, 1),
            "modified": datetime.fromtimestamp(p.stat().st_mtime),
        }
        for p in files
    ]


def dashboard(request):
    """GET / - HTML dashboard that calls the JSON API endpoints."""
    screener_cards = [
        {**s, "url": reverse(s["url_name"]), "fields": _form_fields(s["serializer"])}
        for s in DASHBOARD_SCREENERS
    ]
    techniques = [{
        "value": "top-picks",
        "title": "Top picks (techniques combined)",
        "description": "Stocks where at least 'min signals' techniques agree, ranked by a weighted score.",
    }] + [
        {"value": t["name"], "title": t["title"], "description": t["description"]}
        for t in signals.list_techniques()
    ]
    return render(request, "stocks/dashboard.html", {
        "stats": _dashboard_stats(),
        "csv_files": _csv_files(),
        "screeners": screener_cards,
        "signals": {
            "techniques": techniques,
            "fields": _form_fields(serializers.TopPicksQuerySerializer),
            "url_template": reverse("signal-technique", args=["__technique__"]),
        },
        "urls": {
            "sync": reverse("sync-instruments"),
            "fetch": reverse("fetch-historic"),
            "fetchProgress": reverse("fetch-progress"),
            "fetchCancel": reverse("fetch-cancel"),
            "insert": reverse("insert-data"),
            "engulfing": reverse("bullish-engulfing-symbols"),
            "stock": reverse("signal-stock", args=["__symbol__"]),
            "news": reverse("news-scan"),
            "recommend": reverse("recommendation"),
            "recommendSave": reverse("recommendation-save"),
            "tracked": reverse("tracked-signals"),
            "trackedDates": reverse("tracked-signal-dates"),
            "trackedVerify": reverse("tracked-signals-verify"),
        },
    })


class IndexView(APIView):
    """GET /api - list the available endpoints."""

    def get(self, request):
        from stocks.urls import urlpatterns

        return Response({
            p.name: {"path": f"/{p.pattern}", "methods": sorted(
                m.upper() for m in p.callback.view_class.http_method_names
                if m not in ("options", "head") and hasattr(p.callback.view_class, m)
            )}
            for p in urlpatterns if p.name != "index" and hasattr(p.callback, "view_class")
        })


class SyncInstrumentsView(APIView):
    """POST /stocknames - download the Angel One scrip master into `instruments`."""

    def post(self, request):
        try:
            count = instruments.sync_instruments()
        except Exception as exc:
            logger.exception("Instrument sync failed")
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({"message": "Stock data saved successfully", "count": count})


class FetchHistoricView(APIView):
    """POST /fetch-smart-historic - start fetching daily candles for all BSE/NSE instruments.

    The fetch runs in the background; poll /fetch-smart-historic/progress for counts.
    """

    def post(self, request):
        serializer = serializers.FetchHistoricSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        fmt = serializers.SMARTAPI_DATETIME_FORMAT
        try:
            smartapi.get_client()  # fail fast on bad credentials instead of inside the thread
            job = fetch_jobs.start(
                serializer.validated_data["start_date"].strftime(fmt),
                serializer.validated_data["end_date"].strftime(fmt),
            )
        except SmartApiLoginError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        except fetch_jobs.FetchAlreadyRunning as exc:
            return Response({**fetch_jobs.progress(), "error": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(job, status=status.HTTP_202_ACCEPTED)


class FetchProgressView(APIView):
    """GET /fetch-smart-historic/progress - counts for the running (or last) fetch."""

    def get(self, request):
        return Response(fetch_jobs.progress())


class FetchCancelView(APIView):
    """POST /fetch-smart-historic/cancel - stop the running fetch, keeping what was fetched."""

    def post(self, request):
        if not fetch_jobs.cancel():
            return Response({"error": "No fetch is running"}, status=status.HTTP_409_CONFLICT)
        return Response({"message": "Stopping fetch", **fetch_jobs.progress()})


class InsertDataView(APIView):
    """POST /insert-data - bulk insert a fetched CSV into `historicstocks`."""

    def post(self, request):
        serializer = serializers.InsertDataSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            csv_path = historic.resolve_csv(serializer.validated_data.get("csv_file"))
        except (ValueError, FileNotFoundError) as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        rows = historic.import_historic_csv(csv_path)
        return Response({"message": "data inserted successfully", "csv_file": csv_path.name, "rows": rows})


class BullishEngulfingSymbolsView(APIView):
    """GET /get_engulfing - symbols whose last two candles are a bullish engulfing."""

    def get(self, request):
        return Response(screeners.bullish_engulfing_symbols())


class ScreenerView(APIView):
    """GET view that validates query params and passes them to a screener function."""

    query_serializer = None
    screener = None

    def get(self, request):
        serializer = self.query_serializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        return Response(self.screener(**serializer.validated_data))


class StrongSupportZonesView(ScreenerView):
    """GET /support-zones/strong-only"""

    query_serializer = serializers.SupportZoneQuerySerializer
    screener = staticmethod(screeners.strong_support_zones)


class SwingZonesView(ScreenerView):
    """GET /swing-trading/zones"""

    query_serializer = serializers.SwingZoneQuerySerializer
    screener = staticmethod(screeners.swing_support_zones)


class SupportBullishEngulfingView(ScreenerView):
    """GET /swing-trading/bullish-engulfing"""

    query_serializer = serializers.BullishEngulfingQuerySerializer
    screener = staticmethod(screeners.support_bullish_engulfing)


class SignalTechniquesView(APIView):
    """GET /signals - list the buy-signal techniques."""

    def get(self, request):
        return Response([
            {**t, "url": reverse("signal-technique", args=[t["name"]])} for t in signals.list_techniques()
        ])


def _signal_response(run, params):
    """Run a technique; with as_of, also check each pick against the candles after that date."""
    try:
        result = run(**params)
    except signals.NoCandles as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    if params.get("as_of"):
        result = outcomes.add_outcomes(result)
    return Response(result)


class SignalTechniqueView(APIView):
    """GET /signals/<technique> - stocks where one technique fires on the latest candle.

    ?as_of=2026-10-05 predicts with candles up to that date and adds, for each pick, whether
    and on which date it later reached its target or stop loss.
    """

    def get(self, request, technique):
        if technique not in signals.TECHNIQUES:
            return Response(
                {"error": f"Unknown technique '{technique}'", "techniques": list(signals.TECHNIQUES)},
                status=status.HTTP_404_NOT_FOUND,
            )
        query = serializers.SignalQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        return _signal_response(lambda **p: signals.run_technique(technique, **p), query.validated_data)


class TopPicksView(APIView):
    """GET /signals/top-picks - stocks where several techniques agree, ranked by weighted score.

    Takes ?as_of= like /signals/<technique>.
    """

    def get(self, request):
        query = serializers.TopPicksQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        return _signal_response(signals.top_picks, query.validated_data)


class StockSignalsView(APIView):
    """GET /signals/stock/<symbol> - all indicators and signals for one symbol."""

    def get(self, request, symbol):
        result = signals.stock_signals(symbol)
        if result is None:
            return Response({"error": f"No candles for '{symbol}'"}, status=status.HTTP_404_NOT_FOUND)
        return Response(result)


class TrackedSignalsView(APIView):
    """GET /tracked-signals - saved signals plus hit-rate summary (?status=open|target_hit|stop_hit).
    POST /tracked-signals - save a signal: {"symbol", "buy_price", "target", "stop_loss"?, ...}.
    """

    def get(self, request):
        qs = TrackedSignal.objects.all()
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        if request.query_params.get("signal_date"):
            qs = qs.filter(signal_date=request.query_params["signal_date"])
        if request.query_params.get("technique"):
            qs = qs.filter(technique=request.query_params["technique"])
        return Response({
            "summary": tracking.summary(qs),
            "results": serializers.TrackedSignalSerializer(qs, many=True).data,
        })

    def post(self, request):
        serializer = serializers.TrackedSignalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        signal = tracking.verify(serializer.save())  # an older signal date may already have hit
        return Response(serializers.TrackedSignalSerializer(signal).data, status=status.HTTP_201_CREATED)


class TrackedSignalView(APIView):
    """GET / DELETE /tracked-signals/<id>"""

    def get(self, request, pk):
        signal = TrackedSignal.objects.filter(pk=pk).first()
        if signal is None:
            return Response({"error": f"No tracked signal #{pk}"}, status=status.HTTP_404_NOT_FOUND)
        return Response(serializers.TrackedSignalSerializer(signal).data)

    def delete(self, request, pk):
        deleted, _ = TrackedSignal.objects.filter(pk=pk).delete()
        if not deleted:
            return Response({"error": f"No tracked signal #{pk}"}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


class VerifyTrackedSignalsView(APIView):
    """POST /tracked-signals/verify - re-check saved signals against the latest candles.

    Body filters (all optional): {"signal_date": "2026-10-09"} or {"from_date", "to_date"},
    "technique", "ids". The summary covers only the verified signals.
    """

    def post(self, request):
        query = serializers.VerifySignalsSerializer(data=request.data)
        query.is_valid(raise_exception=True)
        qs = query.filter(TrackedSignal.objects.all())
        verified = tracking.verify_all(qs)
        return Response({
            "verified": len(verified),
            "filters": {k: str(v) for k, v in query.validated_data.items()},
            "summary": tracking.summary(qs),
            "by_technique": tracking.summary_by_technique(qs),
            "results": serializers.TrackedSignalSerializer(verified, many=True).data,
        })


class TrackedSignalDatesView(APIView):
    """GET /tracked-signals/dates - predictions grouped by date, with how each day's picks turned out.

    ?technique=recommendation limits it to one technique.
    """

    def get(self, request):
        qs = TrackedSignal.objects.all()
        if request.query_params.get("technique"):
            qs = qs.filter(technique=request.query_params["technique"])
        return Response(tracking.summary_by_date(qs))


class NewsScanView(APIView):
    """GET /news/scan - world + company news judged by Claude, combined with technical signals.

    Each stock gets a news score (-1..+1) and a verdict: buy (technical signal + positive news),
    avoid (negative news), or watch. Query: technique, limit, symbols=TCS,INFY, refresh.
    """

    def get(self, request):
        query = serializers.NewsScanSerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = dict(query.validated_data)
        if not params.get("symbols") and params["technique"] != "top-picks"                 and params["technique"] not in signals.TECHNIQUES:
            return Response(
                {"error": f"Unknown technique '{params['technique']}'", "techniques": ["top-picks", *signals.TECHNIQUES]},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            return Response(news.scan(**params))
        except news.NewsConfigError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except signals.NoCandles as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:  # Claude / network errors
            logger.exception("News scan failed")
            return Response({"error": f"News analysis failed: {exc}"}, status=status.HTTP_502_BAD_GATEWAY)


class RecommendationView(APIView):
    """GET /recommendation - which stocks to buy today and why.

    Ranked by the techniques that beat the market in the saved backtest. Query: count (default 3),
    min_price, min_avg_turnover, news=on to also drop stocks with bad news (needs ANTHROPIC_API_KEY).
    """

    def get(self, request):
        query = serializers.RecommendationSerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        try:
            return Response(recommend.recommend(**query.validated_data))
        except news.NewsConfigError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except signals.NoCandles as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class SaveRecommendationView(APIView):
    """POST /recommendation/save - save the recommendations (same body as the GET query) for later verification.

    They are stored as tracked signals with technique "recommendation" under their candle date; check
    them later with POST /tracked-signals/verify {"technique": "recommendation", "signal_date": ...}.
    """

    def post(self, request):
        query = serializers.RecommendationSerializer(data=request.data)
        query.is_valid(raise_exception=True)
        try:
            result = recommend.recommend(**query.validated_data)
        except news.NewsConfigError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except signals.NoCandles as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({**result, **tracking.save_recommendations(result)}, status=status.HTTP_201_CREATED)
