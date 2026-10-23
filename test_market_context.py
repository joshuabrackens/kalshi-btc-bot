from datetime import datetime, timezone

from market_context import classify_market_regime, parse_utc_datetime, utc_calendar_context


def test_utc_calendar_context_distinguishes_weekdays_weekends_and_time_buckets():
    friday = utc_calendar_context("2026-10-23T04:12:00+00:00")
    saturday = utc_calendar_context("2026-10-24T19:30:00Z")
    assert friday["day_type_utc"] == "weekday"
    assert friday["weekday_utc"] == "Friday"
    assert friday["utc_time_bucket"] == "00:00–05:59 UTC"
    assert saturday["day_type_utc"] == "weekend"
    assert saturday["weekday_utc"] == "Saturday"
    assert saturday["utc_time_bucket"] == "18:00–23:59 UTC"


def test_naive_datetimes_are_treated_as_utc_and_invalid_times_stay_unknown():
    assert parse_utc_datetime(datetime(2026, 10, 23, 14, 0)) == datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc)
    unknown = utc_calendar_context("not-a-timestamp")
    assert unknown["day_type_utc"] == "unknown"
    assert unknown["weekday_utc"] == "unknown"
    assert unknown["utc_time_bucket"] == "unknown"


def test_regime_labels_require_agreement_and_preserve_conflicts():
    breakout = classify_market_regime(
        {"1m": "bullish", "5m": "bullish", "15m": "mixed"},
        {"direction": 1, "label": "higher highs and higher lows"},
        {"state": "bullish_breakout", "direction": 1},
        chase_warning=False,
    )
    assert breakout["trend_regime"] == "breakout_trend"
    assert breakout["volatility_regime"] == "not_flagged"

    conflict = classify_market_regime(
        {"1m": "bullish", "5m": "bullish", "15m": "bearish"},
        {"direction": -1, "label": "lower highs and lower lows"},
        {"state": "bullish_breakout", "direction": 1},
        chase_warning=True,
    )
    assert conflict["trend_regime"] == "conflicted"
    assert conflict["volatility_regime"] == "elevated_warning"


def test_range_context_and_missing_context_are_not_overstated():
    ranging = classify_market_regime(
        {"1m": "mixed", "5m": "mixed", "15m": "mixed"},
        {"direction": 0, "label": "mixed / range-like swing structure"},
        {"state": "inside_box", "direction": 0},
    )
    missing = classify_market_regime(None, None, None)
    assert ranging["trend_regime"] == "range_or_consolidation"
    assert ranging["volatility_regime"] == "unknown"
    assert missing["trend_regime"] == "unknown"
    assert missing["volatility_regime"] == "unknown"
