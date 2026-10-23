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

    def tearDown(self):
        if self.old_interval is None:
            __import__("os").environ.pop("KALSHI_MIN_REQUEST_INTERVAL_SECONDS", None)
        else:
            __import__("os").environ["KALSHI_MIN_REQUEST_INTERVAL_SECONDS"] = self.old_interval

    def test_retries_rate_limit_then_succeeds(self):
        with patch.object(kalshi_api._SESSION, "get", side_effect=[FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, {"markets": []})]) as get, \
             patch.object(kalshi_api.time, "sleep"):
            result = kalshi_api.get_markets()
        self.assertEqual(result, {"markets": []})
        self.assertEqual(get.call_count, 2)

    def test_does_not_retry_non_transient_400(self):
        with patch.object(kalshi_api._SESSION, "get", return_value=FakeResponse(400)) as get, \
             patch.object(kalshi_api.time, "sleep"):
            with self.assertRaises(kalshi_api.KalshiAPIError):
                kalshi_api.get_market("KXBTC15M-test")
        self.assertEqual(get.call_count, 1)

    def test_request_spacing_helper_respects_disabled_interval(self):
        with patch.object(kalshi_api.time, "sleep") as sleep:
            kalshi_api._wait_for_request_slot()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
