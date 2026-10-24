"""Offline tests for bounded retry and spacing in kalshi_api.py."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import requests

MODULE_PATH = Path(__file__).with_name("kalshi_api.py")
SPEC = importlib.util.spec_from_file_location("kalshi_api_retry_tested", MODULE_PATH)
assert SPEC and SPEC.loader
kalshi_api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kalshi_api)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {"ok": True}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            request = requests.Request("GET", "https://example.invalid").prepare()
            response = requests.Response()
            response.status_code = self.status_code
            response.request = request
            response.url = request.url
            raise requests.HTTPError(f"HTTP {self.status_code}", response=response, request=request)

    def json(self):
        return self._payload


class KalshiApiRetryTests(unittest.TestCase):
    def setUp(self):
        self.old_interval = __import__("os").environ.get("KALSHI_MIN_REQUEST_INTERVAL_SECONDS")
        __import__("os").environ["KALSHI_MIN_REQUEST_INTERVAL_SECONDS"] = "0"
        kalshi_api.MAX_RETRIES = 2
        kalshi_api._NEXT_REQUEST_AT = 0.0
        kalshi_api._RATE_LIMITED_UNTIL = 0.0
        self.fake_monotonic = [100.0]

    def tearDown(self):
        if self.old_interval is None:
            __import__("os").environ.pop("KALSHI_MIN_REQUEST_INTERVAL_SECONDS", None)
        else:
            __import__("os").environ["KALSHI_MIN_REQUEST_INTERVAL_SECONDS"] = self.old_interval

    def test_retries_rate_limit_then_succeeds(self):
        def fake_sleep(seconds):
            self.fake_monotonic[0] += seconds

        with patch.object(kalshi_api._SESSION, "get", side_effect=[
                FakeResponse(429, headers={"Retry-After": "0"}),
                FakeResponse(200, {"markets": []}),
             ]) as get, \
             patch.object(kalshi_api.time, "monotonic", side_effect=lambda: self.fake_monotonic[0]), \
             patch.object(kalshi_api.time, "sleep", side_effect=fake_sleep):
            result = kalshi_api.get_markets()
        self.assertEqual(result, {"markets": []})
        self.assertEqual(get.call_count, 2)
        self.assertEqual(kalshi_api._RATE_LIMITED_UNTIL, 0.0)

    def test_429_cooldown_is_shared_by_later_requests_and_respects_long_retry_after(self):
        kalshi_api.MAX_RETRIES = 0
        sleep_durations = []

        def fake_sleep(seconds):
            sleep_durations.append(seconds)
            self.fake_monotonic[0] += seconds

        with patch.object(kalshi_api._SESSION, "get", return_value=FakeResponse(429, headers={"Retry-After": "45"})) as get, \
             patch.object(kalshi_api.time, "monotonic", side_effect=lambda: self.fake_monotonic[0]), \
             patch.object(kalshi_api.time, "sleep", side_effect=fake_sleep):
            with self.assertRaises(kalshi_api.KalshiAPIError) as caught:
                kalshi_api.get_market("KXBTC15M-rate-limit")
            self.assertEqual(caught.exception.status_code, 429)
            self.assertTrue(caught.exception.retryable)
            # A separate caller must wait on the shared cooldown before sending.
            kalshi_api._wait_for_request_slot()

        self.assertEqual(get.call_count, 1)
        self.assertIn(45.0, sleep_durations)
        self.assertGreaterEqual(max(sleep_durations), 45.0)
        self.assertLessEqual(kalshi_api._RATE_LIMITED_UNTIL, self.fake_monotonic[0])

    def test_retry_after_respects_server_values_beyond_old_30_second_cap(self):
        self.assertEqual(kalshi_api._retry_after_seconds("120", 0, status_code=429), 120.0)
        self.assertEqual(kalshi_api._retry_after_seconds(None, 0, status_code=429), 30.0)

    def test_does_not_retry_non_transient_400(self):
        with patch.object(kalshi_api._SESSION, "get", return_value=FakeResponse(400)) as get, \
             patch.object(kalshi_api.time, "sleep"):
            with self.assertRaises(kalshi_api.KalshiAPIError):
                kalshi_api.get_market("KXBTC15M-test")
        self.assertEqual(get.call_count, 1)


    def test_http_error_exposes_status_without_echoing_url_or_cursor(self):
        long_cursor = "super-secret-looking-cursor"
        with patch.object(kalshi_api._SESSION, "get", return_value=FakeResponse(400, {"message": long_cursor})) as get, \
             patch.object(kalshi_api.time, "sleep"):
            with self.assertRaises(kalshi_api.KalshiAPIError) as caught:
                kalshi_api.get_markets(cursor=long_cursor)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn(long_cursor, str(caught.exception))
        self.assertEqual(get.call_count, 1)

    def test_retries_transient_5xx_then_succeeds(self):
        with patch.object(kalshi_api._SESSION, "get", side_effect=[FakeResponse(503), FakeResponse(200, {"markets": []})]) as get, \
             patch.object(kalshi_api.time, "sleep"):
            result = kalshi_api.get_markets()
        self.assertEqual(result, {"markets": []})
        self.assertEqual(get.call_count, 2)

    def test_get_events_uses_nested_market_metadata_and_caps_page_size(self):
        payload = {"events": [{"event_ticker": "EVT-1", "category": "Sports", "markets": []}], "cursor": "next"}
        with patch.object(kalshi_api._SESSION, "get", return_value=FakeResponse(200, payload)) as get:
            result = kalshi_api.get_events(status="open", limit=999, cursor="next-cursor", with_nested_markets=True)
        self.assertEqual(result, payload)
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["limit"], 200)
        self.assertEqual(params["status"], "open")
        self.assertEqual(params["cursor"], "next-cursor")
        self.assertEqual(params["with_nested_markets"], "true")

    def test_get_multivariate_events_uses_separate_endpoint_and_caps_page_size(self):
        payload = {"events": [], "cursor": "next"}
        with patch.object(kalshi_api._SESSION, "get", return_value=FakeResponse(200, payload)) as get:
            result = kalshi_api.get_multivariate_events(limit=500, cursor="mve-cursor", with_nested_markets=True)
        self.assertEqual(result, payload)
        self.assertTrue(get.call_args.args[0].endswith("/events/multivariate"))
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["limit"], 200)
        self.assertEqual(params["cursor"], "mve-cursor")
        self.assertEqual(params["with_nested_markets"], "true")

    def test_get_events_tickering_filter_is_bounded(self):
        ids = [f"EVT-{i}" for i in range(100)]
        with patch.object(kalshi_api._SESSION, "get", return_value=FakeResponse(200, {"events": [], "cursor": ""})) as get:
            kalshi_api.get_events(tickers=ids)
        tickers = get.call_args.kwargs["params"]["tickers"].split(",")
        self.assertEqual(len(tickers), 40)

    def test_request_spacing_helper_respects_disabled_interval(self):
        with patch.object(kalshi_api.time, "sleep") as sleep:
            kalshi_api._wait_for_request_slot()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
