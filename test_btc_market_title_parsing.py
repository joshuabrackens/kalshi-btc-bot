"""Regression tests for BTC 15-minute market title parsing."""
from datetime import datetime, timedelta, timezone
import unittest

from paper_worker import _extract_market


class BTCMarketTitleTests(unittest.TestCase):
    def test_valid_bitcoin_above_contract_does_not_need_literal_up_in_title(self):
        close = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        market = {
            "ticker": "KXBTC15M-26OCT241600-00",
            "status": "active",
            "title": "Bitcoin above $110,000 at 4:00 PM ET",
            "floor_strike": 110000,
            "close_time": close,
        }
        parsed = _extract_market(market)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["ticker"], market["ticker"])
        self.assertEqual(parsed["strike"], 110000)

    def test_non_btc_title_is_still_rejected(self):
        close = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        market = {
            "ticker": "KXBTC15M-26OCT241600-00",
            "status": "active",
            "title": "Ethereum above $4,000 at 4:00 PM ET",
            "floor_strike": 110000,
            "close_time": close,
        }
        self.assertIsNone(_extract_market(market))


if __name__ == "__main__":
    unittest.main()
