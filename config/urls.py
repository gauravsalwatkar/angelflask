from django.urls import include, path

urlpatterns = [
    path("", include("stocks.urls")),
]
