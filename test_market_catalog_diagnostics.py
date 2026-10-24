from __future__ import annotations

from diagnose_kalshi_catalog import _summarize


class FakeResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def test_catalog_diagnostic_reports_shape_without_printing_cursor_value():
    secretish_cursor = "cursor-should-never-be-printed-ABC123"
    response = FakeResponse({"events": [{"category": "Sports", "markets": [{"ticker": "KXNBA-TEST"}]}], "cursor": secretish_cursor})
    summary = "\n".join(_summarize("events", response))
    assert "HTTP 200" in summary
    assert "events returned: 1" in summary
    assert "nested market rows: 1" in summary
    assert "categories sampled: Sports" in summary
    assert "cursor present: True" in summary
    assert "cursor length:" in summary
    assert secretish_cursor not in summary


def test_catalog_diagnostic_handles_error_body_without_echoing_body():
    class ErrorResponse(FakeResponse):
        status_code = 502
        def json(self):
            raise ValueError("do not print body")
    summary = "\n".join(_summarize("events", ErrorResponse({})))
    assert "HTTP 502" in summary
    assert "body omitted" in summary
    assert "do not print body" not in summary
