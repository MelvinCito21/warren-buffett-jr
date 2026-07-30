"""Tests for wbj.providers.cache.Cache and wbj.providers.base.Provider."""

import json
from datetime import datetime, timedelta, timezone

import httpx

from wbj.config import Settings
from wbj.providers.base import Provider
from wbj.providers.cache import Cache


# --- Cache ---------------------------------------------------------------


def test_cache_roundtrip(tmp_path):
    c = Cache(tmp_path)
    c.put("NVDA", "profile", {"name": "NVIDIA"})
    assert c.get("NVDA", "profile")["name"] == "NVIDIA"
    assert c.age_days("NVDA", "profile") < 1 / 24


def test_cache_get_missing_returns_none(tmp_path):
    c = Cache(tmp_path)
    assert c.get("NVDA", "profile") is None


def test_cache_age_days_missing_returns_none(tmp_path):
    c = Cache(tmp_path)
    assert c.age_days("NVDA", "profile") is None


def test_cache_writes_expected_file_layout(tmp_path):
    c = Cache(tmp_path)
    c.put("NVDA", "profile", {"name": "NVIDIA"})
    path = tmp_path / "NVDA" / "profile.json"
    assert path.exists()
    record = json.loads(path.read_text())
    assert record["payload"] == {"name": "NVIDIA"}
    assert "fetched_at" in record
    # fetched_at must parse as an ISO-8601 UTC timestamp
    parsed = datetime.fromisoformat(record["fetched_at"])
    assert parsed.tzinfo is not None


# --- Provider.get_json: cache-first --------------------------------------


def test_get_json_serves_from_cache_without_hitting_transport(tmp_path):
    cache = Cache(tmp_path)
    cache.put("NVDA", "provider_profile", {"name": "NVIDIA"})

    def handler(request):
        raise AssertionError("transport should not be called on cache hit")

    p = Provider(
        settings=None,
        cache=cache,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = p.get_json("https://x.test/a", {}, "profile", "NVDA")
    assert result == {"name": "NVIDIA"}


def test_get_json_refetches_when_cache_stale(tmp_path):
    cache = Cache(tmp_path)
    cache.put("NVDA", "provider_profile", {"name": "OLD"})
    path = tmp_path / "NVDA" / "provider_profile.json"
    record = json.loads(path.read_text())
    record["fetched_at"] = (
        datetime.now(timezone.utc) - timedelta(days=10)
    ).isoformat()
    path.write_text(json.dumps(record))

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"name": "NEW"})

    p = Provider(
        settings=None,
        cache=cache,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = p.get_json("https://x.test/a", {}, "profile", "NVDA", max_age_days=1)
    assert result == {"name": "NEW"}
    assert calls["n"] == 1


def test_get_json_serves_fresh_cache_within_max_age(tmp_path):
    cache = Cache(tmp_path)
    cache.put("NVDA", "provider_profile", {"name": "FRESH"})

    def handler(request):
        raise AssertionError("transport should not be called for fresh cache")

    p = Provider(
        settings=None,
        cache=cache,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = p.get_json("https://x.test/a", {}, "profile", "NVDA", max_age_days=1)
    assert result == {"name": "FRESH"}


# --- Provider.get_json: retry / backoff -----------------------------------


def test_get_json_retries_twice_then_succeeds(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500)
        return httpx.Response(200, json={"name": "NVIDIA"})

    p = Provider(
        settings=None,
        cache=Cache(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    sleeps = []
    p._sleep = lambda seconds: sleeps.append(seconds)

    result = p.get_json("https://x.test/a", {}, "profile", "NVDA")
    assert result == {"name": "NVIDIA"}
    assert calls["n"] == 3
    assert sleeps == [0.5, 1.0]


def test_provider_returns_none_after_3_failures(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500)

    p = Provider(
        settings=None,
        cache=Cache(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    p._sleep = lambda seconds: None

    assert p.get_json("https://x.test/a", {}, "k", "NVDA") is None
    assert calls["n"] == 3


def test_get_json_backoff_schedule_is_exponential(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500)

    p = Provider(
        settings=None,
        cache=Cache(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    sleeps = []
    p._sleep = lambda seconds: sleeps.append(seconds)

    p.get_json("https://x.test/a", {}, "k", "NVDA")
    # 3 total attempts => backoff sleeps only between attempts (2 gaps),
    # no sleep after the final, exhausted attempt.
    assert sleeps == [0.5, 1.0]


def test_get_json_does_not_retry_on_4xx(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404)

    p = Provider(
        settings=None,
        cache=Cache(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    p._sleep = lambda seconds: None

    assert p.get_json("https://x.test/a", {}, "k", "NVDA") is None
    assert calls["n"] == 1


def test_get_json_retries_on_timeout(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"name": "NVIDIA"})

    p = Provider(
        settings=None,
        cache=Cache(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    p._sleep = lambda seconds: None

    result = p.get_json("https://x.test/a", {}, "profile", "NVDA")
    assert result == {"name": "NVIDIA"}
    assert calls["n"] == 2


def test_get_json_passes_through_custom_headers(tmp_path):
    captured = {}

    def handler(request):
        captured["headers"] = request.headers
        return httpx.Response(200, json={"ok": True})

    p = Provider(
        settings=None,
        cache=Cache(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = p.get_json(
        "https://x.test/a", {}, "k", "NVDA", headers={"User-Agent": "test-agent"}
    )

    assert result == {"ok": True}
    assert captured["headers"].get("user-agent") == "test-agent"


def test_get_json_omitted_headers_does_not_break_request(tmp_path):
    """Existing callers that don't pass `headers` must be unaffected."""

    def handler(request):
        return httpx.Response(200, json={"ok": True})

    p = Provider(
        settings=None,
        cache=Cache(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert p.get_json("https://x.test/a", {}, "k", "NVDA") == {"ok": True}


def test_get_json_returns_none_on_malformed_json_body(tmp_path):
    def handler(request):
        return httpx.Response(200, content=b"not json", headers={"content-type": "application/json"})

    p = Provider(
        settings=None,
        cache=Cache(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    p._sleep = lambda seconds: None

    assert p.get_json("https://x.test/a", {}, "k", "NVDA") is None


def test_get_json_never_raises_on_transport_error(tmp_path):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    p = Provider(
        settings=None,
        cache=Cache(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    p._sleep = lambda seconds: None

    assert p.get_json("https://x.test/a", {}, "k", "NVDA") is None


# --- Provider.get_json: caching successful responses ----------------------


def test_get_json_caches_successful_response(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"name": "NVIDIA"})

    cache = Cache(tmp_path)
    p = Provider(
        settings=None,
        cache=cache,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    p.get_json("https://x.test/a", {}, "profile", "NVDA")
    assert cache.get("NVDA", "provider_profile") == {"name": "NVIDIA"}


# --- Provider.get_json: apikey never logged --------------------------------


def test_get_json_does_not_log_apikey_value(tmp_path, caplog):
    def handler(request):
        return httpx.Response(500)

    p = Provider(
        settings=None,
        cache=Cache(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    p._sleep = lambda seconds: None

    with caplog.at_level("WARNING"):
        p.get_json("https://x.test/a", {"apikey": "SUPERSECRET"}, "k", "NVDA")

    assert "SUPERSECRET" not in caplog.text


def test_get_json_does_not_log_token_value(tmp_path, caplog):
    def handler(request):
        return httpx.Response(404)

    p = Provider(
        settings=None,
        cache=Cache(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    p._sleep = lambda seconds: None

    with caplog.at_level("WARNING"):
        p.get_json("https://x.test/a", {"token": "SUPERSECRET"}, "k", "NVDA")

    assert "SUPERSECRET" not in caplog.text


# --- per-provider cache namespacing -----------------------------------------


def test_two_providers_sharing_a_cache_key_do_not_clobber_each_other(tmp_path):
    """FMP and FinnHub both expose `earnings_calendar`. Sharing a cache slot
    made the second provider read back the first one's payload, so a
    cross-source check silently confirmed a figure it never fetched."""
    from wbj.providers.finnhub import FinnhubProvider
    from wbj.providers.fmp import FMPProvider

    cache = Cache(tmp_path)

    def fmp_handler(request):
        return httpx.Response(200, json=[{"source": "fmp"}])

    def finnhub_handler(request):
        return httpx.Response(200, json=[{"source": "finnhub"}])

    fmp = FMPProvider(
        Settings(fmp_api_key="k"), cache,
        client=httpx.Client(transport=httpx.MockTransport(fmp_handler)),
    )
    finnhub = FinnhubProvider(
        Settings(finnhub_api_key="k"), cache,
        client=httpx.Client(transport=httpx.MockTransport(finnhub_handler)),
    )

    assert fmp.earnings_calendar("NVDA") == [{"source": "fmp"}]
    assert finnhub.earnings_calendar("NVDA") == [{"source": "finnhub"}]
    # And the first provider still reads its own payload afterwards.
    assert fmp.earnings_calendar("NVDA") == [{"source": "fmp"}]


def test_cache_namespace_is_derived_from_the_provider_class(tmp_path):
    from wbj.providers.fmp import FMPProvider

    assert Provider(settings=None, cache=Cache(tmp_path)).cache_namespace == "provider"
    assert FMPProvider(Settings(fmp_api_key="k"), Cache(tmp_path)).cache_namespace == "fmp"
