"""Detect the egress IPv4 this process uses for outbound HTTP calls.

Surfaced on the dashboard so you can copy-paste it into Bybit's IP whitelist
without SSH-ing into the container. Cached for 5 minutes — the IP is mostly
stable, and we don't want to hammer the lookup service on every page load.
"""
from __future__ import annotations
import logging
import time
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_TTL_SECONDS = 300

# Multiple providers — try them in order until one responds. Each is a tiny
# plain-text endpoint that returns just the caller's IP.
_LOOKUP_URLS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)

_CACHE: dict[str, object] = {"ip": None, "expires_at": 0.0}


def get_outbound_ip(force_refresh: bool = False) -> Optional[str]:
    """Return the public IPv4 the middleware appears as to remote APIs.

    Cached for `_TTL_SECONDS`. Returns `None` if every lookup fails — the
    caller should treat that as "unknown" and not crash.
    """
    now = time.time()
    if (not force_refresh
            and _CACHE["ip"]
            and now < float(_CACHE["expires_at"])):
        return _CACHE["ip"]  # type: ignore[return-value]

    for url in _LOOKUP_URLS:
        try:
            with httpx.Client(timeout=3.0) as client:
                r = client.get(url)
            if r.status_code == 200:
                ip = r.text.strip()
                if ip:
                    _CACHE["ip"] = ip
                    _CACHE["expires_at"] = now + _TTL_SECONDS
                    return ip
        except httpx.HTTPError as e:
            log.warning("egress-ip lookup failed via %s: %s", url, e)

    log.error("egress-ip lookup failed on all providers; returning None")
    return None


def clear_cache() -> None:
    """Force the next get_outbound_ip() call to re-fetch."""
    _CACHE["ip"] = None
    _CACHE["expires_at"] = 0.0
