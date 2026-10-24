"""Lazily authenticated, process-wide Angel One SmartAPI client.

Angel One sessions end daily, so a client logged in on an earlier day (India time) logs in again
on its next use; long-running processes (the live feed, the automatic paper trader) keep working.
"""
import logging
import threading

import pyotp
from django.conf import settings
from django.utils import timezone
from SmartApi import SmartConnect

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_client = None
_login_day = None


class SmartApiLoginError(RuntimeError):
    pass


def get_client(force_login=False):
    """Return a logged-in SmartConnect client, logging in on first use and again each new day."""
    global _client, _login_day
    with _lock:
        today = timezone.localdate()
        if _client is None or force_login or _login_day != today:
            _client = _login()
            _login_day = today
        return _client


def _login():
    cfg = settings.ANGEL_ONE
    client = SmartConnect(api_key=cfg["API_KEY"])
    totp = pyotp.TOTP(cfg["TOTP_SECRET"]).now()
    session = client.generateSession(cfg["CLIENT_CODE"], cfg["PIN"], totp)
    if not session or not session.get("status"):
        message = session.get("message") if session else "empty response"
        raise SmartApiLoginError(f"Angel One login failed: {message}")
    logger.info("Logged in to Angel One SmartAPI as %s", cfg["CLIENT_CODE"])
    return client
