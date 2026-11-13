"""HTTP hardening for espn-api.

espn-api calls ``requests.get`` with no timeout and no retries, so a single
slow ESPN response can hang the whole MCP server. We swap the ``requests``
module it uses for a small shim backed by a Session that has a default
timeout and retries transient failures (429 / 5xx / connection errors).
"""

from __future__ import annotations

import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import __version__

DEFAULT_TIMEOUT = 20  # seconds, per request


class _RequestsShim:
    """Drop-in for the ``requests`` module as used by espn-api (only ``get``)."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout
        self._local = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            retry = Retry(
                total=3,
                connect=3,
                read=2,
                status=3,
                backoff_factor=0.6,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET"}),
                respect_retry_after_header=True,
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            session.headers["User-Agent"] = f"espn-fantasy-hub/{__version__}"
            self._local.session = session
        return session

    def get(self, url, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        return self._session().get(url, **kwargs)

    def __getattr__(self, name):  # exceptions, etc.
        return getattr(requests, name)


_installed: _RequestsShim | None = None


def install(timeout: float = DEFAULT_TIMEOUT) -> _RequestsShim:
    """Route all espn-api HTTP calls through the hardened shim (idempotent)."""
    global _installed
    from espn_api.requests import espn_requests

    if _installed is None:
        _installed = _RequestsShim(timeout)
    _installed.timeout = timeout
    espn_requests.requests = _installed
    return _installed


def session() -> requests.Session:
    """Hardened session for our own direct calls (league discovery)."""
    return install()._session()
