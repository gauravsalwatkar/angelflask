"""Lazily authenticated, process-wide Angel One SmartAPI client."""
import logging
import threading

import pyotp
from django.conf import settings
from SmartApi import SmartConnect

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_client = None


class SmartApiLoginError(RuntimeError):
    pass


def get_client(force_login=False):
    """Return a logged-in SmartConnect client, logging in on first use."""
    global _client
    with _lock:
        if _client is None or force_login:
            _client = _login()
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
