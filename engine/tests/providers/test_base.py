"""Tests for wbj.providers.base: param redaction in logged requests."""

import logging

import httpx

from wbj.config import Settings
from wbj.providers.base import Provider
from wbj.providers.cache import Cache


def _make_provider(tmp_path, handler):
    settings = Settings()
    cache = Cache(tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Provider(settings, cache, client=client)


def test_redacts_apikey_token_and_api_key_from_client_error_log(tmp_path, caplog):
    """4xx responses log params; apikey/token/api_key must never appear in
    plaintext in the log output — only the '***' mask."""

    def handler(request):
        return httpx.Response(400, json={"error": "bad request"})

    p = _make_provider(tmp_path, handler)

    with caplog.at_level(logging.WARNING):
        result = p.get_json(
            "https://example.com/thing",
            {
                "apikey": "secret-fmp-key",
                "token": "secret-finnhub-key",
                "api_key": "secret-fred-key",
                "symbol": "NVDA",
            },
            "thing",
            "NVDA",
        )

    assert result is None
    log_text = caplog.text
    assert "secret-fmp-key" not in log_text
    assert "secret-finnhub-key" not in log_text
    assert "secret-fred-key" not in log_text
    assert "NVDA" in log_text


# --- last_status: telling a plan block from an empty response ----------------


def test_last_status_records_402_so_a_plan_block_is_distinguishable(tmp_path):
    """A 402 and an empty 200 both make get_json return None; only the
    recorded status separates "blocked by plan" from "genuinely no data"."""
    p = _make_provider(tmp_path, lambda request: httpx.Response(402, json={}))

    assert p.get_json("https://x/e", {}, "estimates", "IREN") is None
    assert p.last_status_for("estimates") == 402


def test_last_status_records_200_for_a_successful_fetch(tmp_path):
    p = _make_provider(tmp_path, lambda request: httpx.Response(200, json=[]))

    p.get_json("https://x/e", {}, "estimates", "IREN")

    assert p.last_status_for("estimates") == 200


def test_last_status_is_none_before_any_fetch(tmp_path):
    p = _make_provider(tmp_path, lambda request: httpx.Response(200, json=[]))

    assert p.last_status_for("estimates") is None


def test_last_status_not_set_on_cache_hit(tmp_path):
    """A cache hit serves data without a request, so it must not overwrite
    the status of the last real fetch."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"v": 1})

    p = _make_provider(tmp_path, handler)
    p.get_json("https://x/e", {}, "thing", "NVDA", max_age_days=99)
    p.last_status.clear()  # forget the first fetch's status

    p.get_json("https://x/e", {}, "thing", "NVDA", max_age_days=99)

    assert calls["n"] == 1  # second call served from cache
    assert p.last_status_for("thing") is None


def test_last_status_is_namespaced_per_provider(tmp_path):
    """Two providers sharing a key must not read each other's status."""
    p = _make_provider(tmp_path, lambda request: httpx.Response(402, json={}))
    p.get_json("https://x/e", {}, "estimates", "IREN")

    class OtherProvider(Provider):
        pass

    other = OtherProvider(p.settings, p.cache, client=p.client)
    assert other.last_status_for("estimates") is None
