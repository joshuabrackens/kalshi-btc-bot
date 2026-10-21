import os, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paper_worker as w

class WorkerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        w.DB_PATH=Path(self.tmp.name)/"test.sqlite3"
        os.environ.update({"TRADING_MODE":"paper","LIVE_TRADING_ENABLED":"false","MAX_TRADE_DOLLARS":"1.00","MAX_DAILY_LOSS_DOLLARS":"10.00"})
    def tearDown(self): self.tmp.cleanup()
    def test_live_mode_fails_closed(self):
        os.environ["LIVE_TRADING_ENABLED"]="true"
        self.assertEqual(w.run_once()["action"],"blocked")
        os.environ["LIVE_TRADING_ENABLED"]="false"
    def test_market_without_explicit_strike_rejected(self):
        m={"market":{"ticker":"KXBTC15M-TEST","status":"active","title":"BTC price up in next 15 mins?","close_time":(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),"yes_ask":50,"no_ask":50}}
        self.assertIsNone(w._extract_market(m))
    def test_market_explicit_strike_accepted(self):
        m={"market":{"ticker":"KXBTC15M-TEST","status":"active","title":"BTC price up in next 15 mins?","close_time":(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),"floor_strike":65000,"yes_ask":50,"no_ask":50}}
        parsed=w._extract_market(m)
        self.assertIsNotNone(parsed); self.assertEqual(parsed["strike"],65000)

    def test_top_bid_levels(self):
        # Test the simplified order-book format
        self.assertEqual(
            w._top_bid({"orderbook": {"yes": [[40, 3], [42, 1]]}}, "yes"),
            42,
        )

        # Test Kalshi's nested orderbook_fp format
        real_format = {
            "orderbook_fp": {
                "yes_dollars": [["0.4200", "3"], ["0.4500", "1"]],
                "no_dollars": [["0.2900", "2"], ["0.3100", "4"]],
            }
        }

        self.assertAlmostEqual(w._top_bid(real_format, "yes"), 45.0)
        self.assertAlmostEqual(w._top_bid(real_format, "no"), 31.0)

        # Empty order books must not invent prices
        empty_book = {
            "orderbook_fp": {
                "yes_dollars": [],
                "no_dollars": [],
            }
        }

        self.assertIsNone(w._top_bid(empty_book, "yes"))
        self.assertIsNone(w._top_bid(empty_book, "no"))

    def test_discovery_without_strike_does_not_open_trade(self):
        old_discover,old_get_market=w.discover,w.get_market
        w.discover=lambda:{"candidates":[{"ticker":"KXBTC15M-TEST"}]}
        w.get_market=lambda ticker:{"market":{"ticker":ticker,"status":"active","title":"BTC price up in next 15 mins?","close_time":(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),"yes_ask":50,"no_ask":50}}
        try:
            result=w.run_once()
            self.assertEqual(result["action"],"wait")
            c=w._connect()
            try: self.assertEqual(c.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0],0)
            finally: c.close()
        finally: w.discover,w.get_market=old_discover,old_get_market

if __name__=="__main__": unittest.main()
