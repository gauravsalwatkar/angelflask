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
from stocks.analysis import (backtest, flow_strategy, live_signals, outcomes, recommend, screeners, signals,
                             simulate)
from stocks.models import HistoricStock, Instrument, TrackedSignal
from stocks.services import (autotrade, backtest_jobs, fetch_jobs, historic, instruments, intraday, money_flow, news,
                             option_levels, paper, smartapi, tracking)
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
        "fetchable": historic.stock_instruments().count(),
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
            "simulate": reverse("simulate", args=["__symbol__"]),
            "tracked": reverse("tracked-signals"),
            "trackedDates": reverse("tracked-signal-dates"),
            "trackedVerify": reverse("tracked-signals-verify"),
            "optionLevels": reverse("option-levels"),
            "optionSync": reverse("option-contracts-sync"),
            "liveEquity": reverse("live-equity"),
            "liveOptions": reverse("live-options"),
            "moneyFlow": reverse("money-flow"),
            "flowCandles": reverse("money-flow-candles"),
            "flowCandlesProgress": reverse("money-flow-candles-progress"),
            "flowCandlesCancel": reverse("money-flow-candles-cancel"),
            "flowBacktest": reverse("money-flow-backtest"),
            "paper": reverse("paper"),
            "paperOrders": reverse("paper-orders"),
            "paperClose": reverse("paper-close", args=[0]),
            "paperReset": reverse("paper-reset"),
            "paperStats": reverse("paper-stats"),
            "paperAutomation": reverse("paper-automation"),
            "strategies": reverse("strategies"),
            "backtest": reverse("strategies-backtest"),
            "backtestProgress": reverse("strategies-backtest-progress"),
            "backtestCancel": reverse("strategies-backtest-cancel"),
        },
    })


def live_screen(request):
    """GET /live - full-screen live buy lists (Equity and Options tabs); refreshes every 5 s."""
    return render(request, "stocks/live.html", {"urls": {"liveEquity": reverse("live-equity"),
                                                         "liveOptions": reverse("live-options"),
                                                         "dashboard": reverse("dashboard")}})


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
        data = serializer.validated_data
        tokens = (historic.missing_tokens(data["start_date"].date(), data["end_date"].date())
                  if data["only_missing"] else None)
        if tokens == []:
            return Response({"error": "Every instrument already has candles in this range"},
                            status=status.HTTP_409_CONFLICT)
        try:
            smartapi.get_client()  # fail fast on bad credentials instead of inside the thread
            job = fetch_jobs.start(
                data["start_date"].strftime(fmt),
                data["end_date"].strftime(fmt),
                tokens=tokens,
                auto_import=data["auto_import"],
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


class SimulateView(APIView):
    """GET /simulate/<symbol> - time machine: replay a trade's next sessions 10,000 times.

    Each replay chains daily candles drawn from the stock's own last year; the same plan is also
    replayed from every past close. Query: entry, target, stop_loss (default: the latest daily
    plan, close / close + 3 ATR / close - 2 ATR), hold, paths, lookback, seed.
    """

    def get(self, request, symbol):
        query = serializers.SimulateQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = dict(query.validated_data)
        snap = signals.get_snapshot()
        row = snap[snap["symbol"].str.upper() == symbol.upper()]
        if row.empty:
            return Response({"error": f"No candles for '{symbol}'"}, status=status.HTTP_404_NOT_FOUND)
        row = row.iloc[0]
        for key, default in (("entry", row["close"]), ("target", row["target"]), ("stop_loss", row["stop_loss"])):
            params.setdefault(key, float(default))
        try:
            result = simulate.simulate(token=row["token"], exchange=row["exchange"], **params)
        except (simulate.NotEnoughCandles, ValueError) as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"symbol": row["symbol"], "exchange": row["exchange"], "plan_as_of": signals._clean(row["date"]),
                         **result})


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


class StrategiesView(APIView):
    """GET /strategies - every strategy with its saved backtest record and whether it is proven.

    Proven = enough trades, beat buying every liquid stock on the same dates, profit factor >= 1;
    these are the strategies the recommendation ranks by.
    """

    def get(self, request):
        bt = backtest.load_results()
        stats = bt["techniques"] if bt else {}
        rows = [{"name": t["name"], "title": t["title"], "description": t["description"],
                 "url": reverse("signal-technique", args=[t["name"]])} for t in signals.list_techniques()]
        rows.append({"name": "top_picks", "title": "Top picks (2+ techniques agree)",
                     "description": "Stocks where at least two techniques fire on the same day.",
                     "url": reverse("signal-top-picks")})
        for row in rows:
            row["stats"] = stats.get(row["name"])
            row["proven"] = bool(row["stats"]) and recommend.is_proven(row["stats"])
        rows.sort(key=lambda r: (not r["proven"], -(r["stats"] or {}).get("excess_return_pct", -1e9)))
        return Response({
            "backtest": {k: bt[k] for k in ("generated_at", "period", "rules", "market", "runtime_seconds") if k in bt}
            if bt else None,
            "criteria": {"min_trades": recommend.MIN_TRADES, "min_profit_factor": recommend.MIN_PROFIT_FACTOR,
                         "excess_return": "> 0 (beats buying every liquid stock on the same dates)"},
            "strategies": rows,
        })


class BacktestView(APIView):
    """POST /strategies/backtest - start a backtest of every strategy in the background.

    Body: step, hold, start_date, end_date (all optional). The result replaces
    DATA_DIR/backtest.json, which decides the proven strategies. Poll /strategies/backtest/progress.
    """

    def post(self, request):
        serializer = serializers.BacktestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            job = backtest_jobs.start(**serializer.validated_data)
        except backtest_jobs.BacktestAlreadyRunning as exc:
            return Response({**backtest_jobs.progress(), "error": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(job, status=status.HTTP_202_ACCEPTED)


class BacktestProgressView(APIView):
    """GET /strategies/backtest/progress - progress of the running (or last) backtest."""

    def get(self, request):
        return Response(backtest_jobs.progress())


class BacktestCancelView(APIView):
    """POST /strategies/backtest/cancel - stop the running backtest; the saved results are kept."""

    def post(self, request):
        if not backtest_jobs.cancel():
            return Response({"error": "No backtest is running"}, status=status.HTTP_409_CONFLICT)
        return Response(backtest_jobs.progress())


class RecommendationView(APIView):
    """GET /recommendation - which stocks to buy today and why.

    Ranked by the techniques that beat the market in the saved backtest. Query: count (default 3),
    min_price, min_avg_turnover, news=on to also drop stocks with bad news (needs ANTHROPIC_API_KEY),
    simulate=false to skip the time machine odds. Each pick is sized to your capital and max loss.
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


class OptionLevelsView(APIView):
    """GET /option-levels - entry, stop loss and targets for NIFTY / BANKNIFTY options.

    Query: underlying=NIFTY|BANKNIFTY, date=2026-10-15 (default today). Each level set also
    says whether the day's later candles reached the entry, the stop loss or the targets.
    """

    def get(self, request):
        query = serializers.OptionLevelsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        try:
            return Response(option_levels.option_levels(
                query.validated_data["underlying"], query.validated_data.get("date")))
        except SmartApiLoginError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        except option_levels.LevelsError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


class OptionContractsSyncView(APIView):
    """POST /option-levels/sync-contracts - store live NIFTY / BANKNIFTY option contracts."""

    def post(self, request):
        try:
            return Response(instruments.sync_index_options())
        except Exception as exc:
            logger.exception("Option contract sync failed")
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


class LiveEquityView(APIView):
    """GET /live/equity - today's recommendations re-checked against live WebSocket prices.

    Query: count=5. Each stock gets BUY / WAIT / AVOID with the reasons.
    """

    def get(self, request):
        query = serializers.LiveEquityQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        try:
            return Response(live_signals.equity_suggestions(query.validated_data["count"]))
        except SmartApiLoginError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        except signals.NoCandles as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class MoneyFlowView(APIView):
    """GET /money-flow - intraday money flow: which liquid stocks are being bought or sold right now.

    Query: count (default 100 most liquid stocks), fno=false to skip the F&O OI build-up lists.
    Each stock: traded value today, relative turnover vs its usual day, price vs VWAP, and a
    buying / selling / mixed reading.
    """

    def get(self, request):
        query = serializers.MoneyFlowQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        try:
            return Response(money_flow.money_flow(**query.validated_data))
        except SmartApiLoginError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        except signals.NoCandles as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class FlowCandlesView(APIView):
    """POST /money-flow/candles - download 5-minute candles of the most liquid NSE stocks in the background.

    Body: count (default 100). The first run fetches ~95 days per stock, later runs only the new
    days; about a second per stock. Poll /money-flow/candles/progress.
    """

    def post(self, request):
        serializer = serializers.FlowCandlesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        universe, loading = money_flow._universe(serializer.validated_data["count"])
        stocks = [{"symbol": u["symbol"], "token": u["token"]} for u in universe if u["exchange"] == "NSE"]
        if not stocks:
            return Response({"error": "The NSE share list is still downloading (a few minutes, once a week); "
                                      "start the money flow, wait for it, then try again" if loading
                             else "None of these stocks has an NSE contract"}, status=status.HTTP_409_CONFLICT)
        try:
            return Response(intraday.start(stocks), status=status.HTTP_202_ACCEPTED)
        except intraday.DownloadAlreadyRunning as exc:
            return Response({**intraday.progress(), "error": str(exc)}, status=status.HTTP_409_CONFLICT)


class FlowCandlesProgressView(APIView):
    """GET /money-flow/candles/progress - progress of the 5-minute candle download, and what is stored."""

    def get(self, request):
        return Response({**intraday.progress(), "stored_stocks": len(intraday.stored_stocks())})


class FlowCandlesCancelView(APIView):
    """POST /money-flow/candles/cancel - stop the download; candles fetched so far are kept."""

    def post(self, request):
        if not intraday.cancel():
            return Response({"error": "No download is running"}, status=status.HTTP_409_CONFLICT)
        return Response(intraday.progress())


class FlowBacktestView(APIView):
    """GET /money-flow/backtest - the saved money-flow strategy backtest (null if none).
    POST - backtest the rules (body: min_rel, min_change_pct, target_r, min_stop_pct, cost_pct) on the
    stored 5-minute candles and save it; the live signals then use these rules.
    """

    def get(self, request):
        return Response(flow_strategy.load_results())

    def post(self, request):
        serializer = serializers.FlowBacktestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            return Response(flow_strategy.run_and_save(serializer.validated_data))
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class PaperView(APIView):
    """GET /paper?book=intraday|short_term|long_term[&date=2026-10-26] - a paper-trading book at live prices.

    Viewing it also runs its exits: stops and targets hit since the entry, and the 15:15 square-off
    of intraday trades. With date, only the trades bought that day are listed, with a day summary;
    `dates` lists the days that have trades.
    """

    def get(self, request):
        query = serializers.PaperBookSerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        try:
            return Response(paper.book(query.validated_data["book"], query.validated_data.get("date")))
        except SmartApiLoginError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


class PaperOrderView(APIView):
    """POST /paper/orders - open a paper trade at the live price (see PaperOrderSerializer). No real order."""

    def post(self, request):
        serializer = serializers.PaperOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            trade = paper.place(**serializer.validated_data)
        except paper.PaperError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except SmartApiLoginError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(paper._row(trade, trade.entry_price), status=status.HTTP_201_CREATED)


class PaperCloseView(APIView):
    """POST /paper/trades/<id>/close - close an open paper trade at the live price."""

    def post(self, request, pk):
        try:
            return Response(paper._row(paper.close(pk)))
        except paper.PaperError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class PaperStatsView(APIView):
    """GET /paper/stats[?book=...] - which techniques work: per technique, target hit %, time to target,
    win rate, P&L and the best / worst move while open."""

    def get(self, request):
        book = request.query_params.get("book")
        if book:
            query = serializers.PaperBookSerializer(data=request.query_params)
            query.is_valid(raise_exception=True)
        return Response(paper.stats(book or None))


class PaperAutomationView(APIView):
    """GET /paper/automation - status and recent actions of the automatic paper trader.
    POST {"action": "start"|"stop"} - start it as a background process, or ask it to stop
    (same as `python manage.py paper_autotrade` / Ctrl+C)."""

    def get(self, request):
        return Response(autotrade.load_status())

    def post(self, request):
        action = request.data.get("action")
        if action == "start":
            try:
                pid = autotrade.launch()
            except RuntimeError as exc:
                return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)
            return Response({"started": True, "pid": pid}, status=status.HTTP_202_ACCEPTED)
        if action == "stop":
            if not autotrade.request_stop():
                return Response({"error": "The automatic paper trader is not running"}, status=status.HTTP_409_CONFLICT)
            return Response({"stopping": True})
        return Response({"error": 'action must be "start" or "stop"'}, status=status.HTTP_400_BAD_REQUEST)


class PaperResetView(APIView):
    """POST /paper/reset {"book": ...} - delete every trade of a paper book and start again."""

    def post(self, request):
        serializer = serializers.PaperBookSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response({"deleted": paper.reset(serializer.validated_data["book"])})


class LiveOptionsView(APIView):
    """GET /live/options - when to buy or add to the NIFTY / BANKNIFTY option legs, from live ticks.

    Query: underlying=NIFTY|BANKNIFTY. Each leg gets WAIT / BUY / HOLD / ADD / BOOK / EXIT / SKIP.
    """

    def get(self, request):
        query = serializers.LiveOptionsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        try:
            return Response(live_signals.option_suggestions(query.validated_data["underlying"]))
        except SmartApiLoginError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        except option_levels.LevelsError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
