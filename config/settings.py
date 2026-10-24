"""Django settings for the Angel One stock screener.

All secrets and environment-specific values are read from `.env`
(see `.env.example`).
"""
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    HISTORIC_FETCH_MAX_THREADS=(int, 2),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")

INSTALLED_APPS = [
    "rest_framework",
    "stocks",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    },
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": env.db("DATABASE_URL"),
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Holds the computed buy-signal snapshot; it is keyed on the data version, so new
# candles invalidate it automatically.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "TIMEOUT": 6 * 60 * 60,
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = False
USE_TZ = True

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "UNAUTHENTICATED_USER": None,
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "loggers": {
        "stocks": {"handlers": ["console"], "level": "INFO"},
    },
}

# --- Angel One SmartAPI ---
ANGEL_ONE = {
    "API_KEY": env("ANGEL_API_KEY"),
    "CLIENT_CODE": env("ANGEL_CLIENT_CODE"),
    "PIN": env("ANGEL_PIN"),
    "TOTP_SECRET": env("ANGEL_TOTP_SECRET"),
    "SCRIP_MASTER_URL": env(
        "ANGEL_SCRIP_MASTER_URL",
        default="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
    ),
}

# --- App ---
DATA_DIR = BASE_DIR / env("DATA_DIR", default="data")
HISTORIC_FETCH_MAX_THREADS = env("HISTORIC_FETCH_MAX_THREADS")

# Position sizing (stocks.analysis.risk): each BUY is sized so the loss at the stop loss stays
# within MAX_LOSS_PER_TRADE and the cost within TRADING_CAPITAL (rupees).
TRADING_CAPITAL = env.float("TRADING_CAPITAL", default=100_000)
MAX_LOSS_PER_TRADE = env.float("MAX_LOSS_PER_TRADE", default=1_000)

# Paper trading (stocks.services.paper): virtual rupees per book (intraday, short-term, long-term).
PAPER_CAPITAL = env.float("PAPER_CAPITAL", default=100_000)

# News analysis (Claude). Without a key the news endpoints return an error explaining what to set.
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")
ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", default="claude-sonnet-5-5")
NEWS_CACHE_SECONDS = env.int("NEWS_CACHE_SECONDS", default=60 * 60)
