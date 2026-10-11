from django.urls import path

from stocks import views

# Paths match the old FastAPI routes so existing clients keep working.
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("api", views.IndexView.as_view(), name="index"),
    path("stocknames", views.SyncInstrumentsView.as_view(), name="sync-instruments"),
    path("fetch-smart-historic", views.FetchHistoricView.as_view(), name="fetch-historic"),
    path("fetch-smart-historic/progress", views.FetchProgressView.as_view(), name="fetch-progress"),
    path("fetch-smart-historic/cancel", views.FetchCancelView.as_view(), name="fetch-cancel"),
    path("insert-data", views.InsertDataView.as_view(), name="insert-data"),
    path("get_engulfing", views.BullishEngulfingSymbolsView.as_view(), name="bullish-engulfing-symbols"),
    path("support-zones/strong-only", views.StrongSupportZonesView.as_view(), name="strong-support-zones"),
    path("swing-trading/zones", views.SwingZonesView.as_view(), name="swing-zones"),
    path(
        "swing-trading/bullish-engulfing",
        views.SupportBullishEngulfingView.as_view(),
        name="support-bullish-engulfing",
    ),
    path("recommendation", views.RecommendationView.as_view(), name="recommendation"),
    path("recommendation/save", views.SaveRecommendationView.as_view(), name="recommendation-save"),
    path("news/scan", views.NewsScanView.as_view(), name="news-scan"),
    # Saved buy signals, verified against later candles
    path("tracked-signals", views.TrackedSignalsView.as_view(), name="tracked-signals"),
    path("tracked-signals/dates", views.TrackedSignalDatesView.as_view(), name="tracked-signal-dates"),
    path("tracked-signals/verify", views.VerifyTrackedSignalsView.as_view(), name="tracked-signals-verify"),
    path("tracked-signals/<int:pk>", views.TrackedSignalView.as_view(), name="tracked-signal"),
    # Buy signals (fixed paths before the <technique> catch-all)
    path("signals", views.SignalTechniquesView.as_view(), name="signal-techniques"),
    path("signals/top-picks", views.TopPicksView.as_view(), name="signal-top-picks"),
    path("signals/stock/<str:symbol>", views.StockSignalsView.as_view(), name="signal-stock"),
    path("signals/<slug:technique>", views.SignalTechniqueView.as_view(), name="signal-technique"),
]
