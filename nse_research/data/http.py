"""HTTP client for NSE/Yahoo with retries, polite pacing and an on-disk raw cache.

Every raw file is written to the cache *before* it is parsed, so a parser bug or a
later outage never forces a re-download, and old days are never fetched twice.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


class NotFound(Exception):
    """The file does not exist (holiday, not yet published, or URL format changed)."""


class FetchError(Exception):
    """The source could not be reached or refused us (blocked, timeout, 5xx)."""


class HttpClient:
    def __init__(self, cache_dir: Path, delay: float = 0.8, timeout: float = 20, retries: int = 3):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self._session: requests.Session | None = None
        self._last_request = 0.0

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            s = requests.Session()
            s.headers.update(BROWSER_HEADERS)
            # NSE's JSON APIs need the cookies its web pages set. The home page sometimes
            # hangs up on scripts; a quote page sets the same cookies, so try both.
            for warm in ("https://www.nseindia.com/",
                         "https://www.nseindia.com/get-quotes/equity?symbol=RELIANCE"):
                try:
                    s.get(warm, timeout=self.timeout)
                    if s.cookies:
                        break
                except requests.RequestException as e:
                    log.debug("NSE warm-up %s failed (often fine for archive files): %s", warm, e)
            self._session = s
        return self._session

    def _pace(self) -> None:
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def get_cached(self, url: str, cache_name: str, refresh: bool = False, html: bool = False) -> bytes:
        """Return file bytes, from cache if present, else download and cache.
        html=True for web pages (Screener), where an HTML body is the expected answer."""
        path = self.cache_dir / cache_name
        if path.exists() and not refresh and path.stat().st_size > 0:
            return path.read_bytes()
        data = self.get(url, html=html)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        return data

    def get(self, url: str, html: bool = False) -> bytes:
        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._pace()
            try:
                r = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as e:
                last_err = e
                log.warning("GET %s failed (attempt %d): %s", url, attempt, e)
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 404:
                raise NotFound(url)
            if r.status_code in (401, 403):
                # Blocked. Reset the session (new cookies) and retry once more slowly.
                last_err = FetchError(f"HTTP {r.status_code} (blocked?) for {url}")
                self._session = None
                time.sleep(3 * attempt)
                continue
            if r.status_code >= 400:
                last_err = FetchError(f"HTTP {r.status_code} for {url}")
                time.sleep(2 ** attempt)
                continue
            body = r.content
            # NSE sometimes answers 200 with an HTML error page instead of the file.
            if not html and body.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
                raise NotFound(f"HTML page instead of file: {url}")
            return body
        raise FetchError(str(last_err))
