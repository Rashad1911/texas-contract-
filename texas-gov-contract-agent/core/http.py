"""One place for outbound HTTP: polite user agent, timeouts, and retries."""
from __future__ import annotations

import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

USER_AGENT = ("TexasContractScout/1.0 (small-business procurement research; "
              "reads public solicitation listings only)")
DEFAULT_TIMEOUT = 45


def session(retry_statuses=(500, 502, 503, 504), retries: int = 3) -> requests.Session:
    """A requests session with retries. Pass retry_statuses=() for metered APIs (e.g. SAM.gov)
    so a failed call is not silently retried against your daily quota."""
    s = requests.Session()
    retry = Retry(total=retries if retry_statuses else 0, backoff_factor=1.5,
                  status_forcelist=list(retry_statuses), allowed_methods=["GET", "POST"],
                  raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": USER_AGENT,
                      "Accept": "application/json, text/html;q=0.9, */*;q=0.8"})
    return s


_shared = None


def shared() -> requests.Session:
    global _shared
    if _shared is None:
        _shared = session()
    return _shared


def get(url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return shared().get(url, **kwargs)


def post(url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return shared().post(url, **kwargs)
