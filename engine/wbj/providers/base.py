"""Resilient HTTP provider base: cache-first fetch with retry/backoff.

`Provider.get_json` and `Provider.get_text` never raise for network/HTTP
failures — they return `None` on exhaustion, and callers are expected to
map that to `wbj.core.nullstates.NullState.MISSING`.

`get_text` exists because SEC EDGAR serves filings (Form 4, 13F-HR) as
XML documents rather than JSON. It shares the same cache-first and
retry/backoff path as `get_json`; only the decode step differs.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from wbj.providers.cache import Cache

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (0.5, 1.0, 2.0)
_REDACTED_PARAMS = frozenset({"apikey", "token", "api_key"})


def _redact_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Copy `params` with sensitive values masked, safe to put in log text."""
    if not params:
        return {}
    return {
        k: ("***" if k.lower() in _REDACTED_PARAMS else v) for k, v in params.items()
    }


def _decode_json(response: httpx.Response) -> dict | None:
    """Decode a response as JSON, or None if the body isn't valid JSON."""
    try:
        return response.json()
    except ValueError:
        return None


def _decode_text(response: httpx.Response) -> dict | None:
    """Wrap a response's text body for caching. Never fails."""
    return {"text": response.text}


class Provider:
    """Base class for wbj data providers.

    Subclasses build request URLs/params and call `get_json`, which
    handles cache-first serving and resilient retries uniformly.
    """

    def __init__(
        self,
        settings: Any,
        cache: Cache,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.client = client if client is not None else httpx.Client()
        # HTTP status of the last *network* fetch per namespaced cache key.
        # Lets a caller tell why `get_json` returned None: a 402 (endpoint
        # or ticker outside the plan) reads very differently from a 200
        # with an empty body (the company genuinely has no such data). A
        # cache hit records nothing — the key stays absent — because a hit
        # means data was served, not that a request just failed.
        self.last_status: dict[str, int] = {}

    def last_status_for(self, cache_key: str) -> int | None:
        """HTTP status of the last network fetch for `cache_key`, or None.

        None means either no fetch happened this run (a cache hit served
        the data, or the provider was unavailable) or the request never
        got a response (a transport error). Callers pass the un-namespaced
        key they gave `get_json`; the provider's namespace is applied here.
        """
        return self.last_status.get(f"{self.cache_namespace}_{cache_key}")

    @property
    def cache_namespace(self) -> str:
        """Per-provider prefix for cache keys.

        `Cache` stores at `<ticker>/<key>.json`, so two providers offering the
        same data under the same key (FMP and FinnHub both have
        `earnings_calendar`) overwrite each other's entries and each reads back
        the other's payload. That silently defeats cross-source verification:
        the second provider "confirms" a figure it never fetched.
        """
        return type(self).__name__.removesuffix("Provider").lower() or "provider"

    def _sleep(self, seconds: float) -> None:
        """Sleep for `seconds`. Isolated so tests can monkeypatch it out."""
        time.sleep(seconds)

    def get_json(
        self,
        url: str,
        params: dict[str, Any],
        cache_key: str,
        ticker: str,
        max_age_days: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict | None:
        """Fetch JSON, cache-first, with retry/backoff on transient failures.

        If a cache entry exists for (ticker, cache_key) and is fresh enough
        (age <= max_age_days, or max_age_days is None), it is returned
        without touching the network. Otherwise up to 3 attempts are made
        against `url`, backing off 0.5s/1s/2s between attempts on 5xx
        responses or httpx transport errors (including timeouts). 4xx
        responses are treated as non-retryable client errors. Returns None
        (never raises) if the fetch ultimately fails; a successful response
        is written to cache before being returned.

        `headers`, if given, is passed through to the underlying request
        (e.g. a required `User-Agent` per SEC EDGAR's fair-access policy).
        Existing callers that don't pass `headers` are unaffected.
        """
        return self._fetch(
            url, params, cache_key, ticker, max_age_days, headers, _decode_json
        )

    def get_text(
        self,
        url: str,
        params: dict[str, Any],
        cache_key: str,
        ticker: str,
        max_age_days: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> str | None:
        """Fetch a text document (e.g. an EDGAR filing's XML), cache-first.

        Identical cache and retry semantics to `get_json`; the body is kept
        as text instead of being JSON-decoded. The cache stores it wrapped
        as `{"text": ...}` so a cached document stays distinguishable from a
        cached JSON payload — reading one back as the other would otherwise
        fail far from the cause.
        """
        payload = self._fetch(
            url, params, cache_key, ticker, max_age_days, headers, _decode_text
        )
        if not isinstance(payload, dict):
            return None
        text = payload.get("text")
        return text if isinstance(text, str) else None

    def _fetch(
        self,
        url: str,
        params: dict[str, Any],
        cache_key: str,
        ticker: str,
        max_age_days: float | None,
        headers: dict[str, str] | None,
        decode: "Callable[[httpx.Response], dict | None]",
    ) -> dict | None:
        """Cache-first fetch with retry/backoff, shared by get_json/get_text.

        `decode` turns a successful response into the JSON-serializable
        payload to cache and return, or None if the body is unusable.
        """
        cache_key = f"{self.cache_namespace}_{cache_key}"
        age = self.cache.age_days(ticker, cache_key)
        if age is not None and (max_age_days is None or age <= max_age_days):
            return self.cache.get(ticker, cache_key)

        safe_params = _redact_params(params)

        for attempt in range(_MAX_ATTEMPTS):
            is_last_attempt = attempt == _MAX_ATTEMPTS - 1
            try:
                response = self.client.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                logger.warning(
                    "wbj provider request failed (attempt %d/%d) url=%s "
                    "params=%s error=%s",
                    attempt + 1,
                    _MAX_ATTEMPTS,
                    url,
                    safe_params,
                    exc,
                )
                if not is_last_attempt:
                    self._sleep(_BACKOFF_SECONDS[attempt])
                continue

            self.last_status[cache_key] = response.status_code

            if response.status_code < 400:
                payload = decode(response)
                if payload is None:
                    logger.warning(
                        "wbj provider returned malformed JSON status=%d url=%s "
                        "params=%s",
                        response.status_code,
                        url,
                        safe_params,
                    )
                    return None
                self.cache.put(ticker, cache_key, payload)
                return payload

            if response.status_code < 500:
                logger.warning(
                    "wbj provider client error status=%d url=%s params=%s",
                    response.status_code,
                    url,
                    safe_params,
                )
                return None

            logger.warning(
                "wbj provider server error (attempt %d/%d) status=%d url=%s "
                "params=%s",
                attempt + 1,
                _MAX_ATTEMPTS,
                response.status_code,
                url,
                safe_params,
            )
            if not is_last_attempt:
                self._sleep(_BACKOFF_SECONDS[attempt])

        return None

