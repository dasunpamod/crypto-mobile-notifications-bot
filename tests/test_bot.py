"""Smoke tests for the crypto-alerts bot (stdlib only, no network).

Run:  python -m unittest discover -s tests -v
"""

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(coro):
    return asyncio.run(coro)


class TestSymbolNormalization(unittest.TestCase):
    def test_normalize(self):
        import database as db
        self.assertEqual(db.normalize_symbol("btc"), "BTCUSDT")
        self.assertEqual(db.normalize_symbol("ethusdt"), "ETHUSDT")
        self.assertEqual(db.normalize_symbol(" sol "), "SOLUSDT")
        self.assertTrue(db.is_valid_symbol("BTCUSDT"))
        self.assertTrue(db.is_valid_symbol("btc"))
        self.assertFalse(db.is_valid_symbol("!!!"))
        self.assertFalse(db.is_valid_symbol(""))

    def test_format_price(self):
        from telegram_bot import format_price
        self.assertEqual(format_price(70000), "$70,000.00")
        self.assertEqual(format_price(2.5), "$2.50")
        self.assertEqual(format_price(0.5), "$0.5000")
        self.assertEqual(format_price(0.1234), "$0.1234")
        self.assertEqual(format_price(0.0001234), "$0.000123")
        self.assertEqual(format_price(0.00004567), "$0.000046")
        self.assertEqual(format_price(0.00000123), "$0.00000123")
        self.assertEqual(format_price(-5), "N/A")
        self.assertEqual(format_price(float("nan")), "N/A")
        self.assertEqual(format_price("junk"), "N/A")


class TestDatabase(unittest.TestCase):
    def test_crud_roundtrip(self):
        async def go():
            import database as db
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                aid = await db.add_alert("btc", 70000, "above", False)
                self.assertGreater(aid, 0)
                row = await db.get_alert(aid)
                self.assertIsNotNone(row)
                self.assertEqual(row[1], "BTCUSDT")
                self.assertEqual(await db.count_alerts(), 1)
                self.assertEqual(await db.get_active_symbols(), {"BTCUSDT"})
                self.assertTrue(await db.remove_alert(aid))
                self.assertEqual(await db.count_alerts(), 0)
                with self.assertRaises(ValueError):
                    await db.add_alert("!!!", 100, "above")
                with self.assertRaises(ValueError):
                    await db.add_alert("BTCUSDT", -1, "above")
                with self.assertRaises(ValueError):
                    await db.add_alert("BTCUSDT", 100, "sideways")
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())

    def test_checkpoint_wal(self):
        async def go():
            import database as db
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                await db.add_alert("BTCUSDT", 70000, "above")
                await db.checkpoint_wal()
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())


class TestAlertEngine(unittest.TestCase):
    def test_one_shot_fires_once(self):
        async def go():
            import database as db
            from alert_engine import AlertEngine
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                engine = AlertEngine()
                sent = []

                async def fake_notify(**kwargs):
                    sent.append(kwargs)

                import alert_engine as ae
                old_notify = ae.send_alert_notification
                ae.send_alert_notification = fake_notify
                try:
                    await db.add_alert("BTCUSDT", 70000, "above", False)
                    await engine.on_price_update("BTCUSDT", 69000)
                    self.assertEqual(len(sent), 0)
                    await engine.on_price_update("BTCUSDT", 71000)
                    self.assertEqual(len(sent), 1)
                    self.assertEqual(await db.count_alerts(), 0)
                    await engine.on_price_update("BTCUSDT", float("nan"))
                finally:
                    ae.send_alert_notification = old_notify
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())

    def test_persistent_cooldown(self):
        async def go():
            import database as db
            from alert_engine import AlertEngine
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                engine = AlertEngine()
                sent = []

                async def fake_notify(**kwargs):
                    sent.append(kwargs)

                import alert_engine as ae
                old_notify = ae.send_alert_notification
                ae.send_alert_notification = fake_notify
                try:
                    await db.add_alert("ETHUSDT", 3000, "above", True)
                    await engine.on_price_update("ETHUSDT", 3100)
                    self.assertEqual(len(sent), 1)
                    await engine.on_price_update("ETHUSDT", 3200)
                    self.assertEqual(len(sent), 1)
                    self.assertEqual(await db.count_alerts(), 1)
                finally:
                    ae.send_alert_notification = old_notify
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())

    def test_muted_still_tracks_price(self):
        async def go():
            import database as db
            from alert_engine import AlertEngine
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                engine = AlertEngine()
                engine.pause_alerts(1)
                await engine.on_price_update("BTCUSDT", 65000)
                self.assertEqual(engine.last_prices.get("BTCUSDT"), 65000)
                engine.resume_alerts()
                self.assertFalse(engine.is_muted())
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())

    def test_move_alert_fires_on_rapid_move(self):
        async def go():
            import database as db
            from alert_engine import AlertEngine
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                engine = AlertEngine()
                sent = []

                async def fake_notify(**kwargs):
                    sent.append(kwargs)

                import alert_engine as ae
                old_notify = ae.send_alert_notification
                ae.send_alert_notification = fake_notify
                try:
                    await db.add_alert("BTCUSDT", 100, "above", True,
                                       alert_type="move", pct=5.0, window_min=5,
                                       base_price=100.0, peak_price=100.0)
                    # +2% move within window -> no fire
                    await engine.on_price_update("BTCUSDT", 102.0)
                    self.assertEqual(len(sent), 0)
                    # +6% move within window -> fire!
                    await engine.on_price_update("BTCUSDT", 106.0)
                    self.assertEqual(len(sent), 1)
                    self.assertIn("+6.00% in 5m", sent[0].get("detail", ""))
                finally:
                    ae.send_alert_notification = old_notify
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())

    def test_move_alert_reanchors_on_window_expiry(self):
        async def go():
            import database as db
            from alert_engine import AlertEngine
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                engine = AlertEngine()
                sent = []

                async def fake_notify(**kwargs):
                    sent.append(kwargs)

                import alert_engine as ae
                import datetime as _dt
                old_notify = ae.send_alert_notification
                ae.send_alert_notification = fake_notify
                try:
                    aid = await db.add_alert("BTCUSDT", 100, "above", True,
                                             alert_type="move", pct=5.0, window_min=5,
                                             base_price=100.0, peak_price=100.0)
                    # Simulate alert created 10 minutes ago
                    ten_min_ago = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=10)).isoformat()
                    conn = await db.get_db()
                    await conn.execute("UPDATE alerts SET created_at = ? WHERE id = ?", (ten_min_ago, aid))
                    await conn.commit()

                    # Price ticks to 102.0 (+2%, under 5% threshold).
                    # Window expired -> re-anchors base_price to 102.0 and sets last_triggered_at to now.
                    await engine.on_price_update("BTCUSDT", 102.0)
                    self.assertEqual(len(sent), 0)

                    row = await db.get_alert(aid)
                    # base_price (index 13) should now be 102.0
                    self.assertEqual(row[13], 102.0)
                    self.assertIsNotNone(row[6])  # last_triggered_at updated

                    # Next tick to 102.5 (+0.49%) right after -> window is fresh!
                    # Should NOT rebase again!
                    await engine.on_price_update("BTCUSDT", 102.5)
                    row2 = await db.get_alert(aid)
                    self.assertEqual(row2[13], 102.0)  # still 102.0, not rebased on every tick!
                finally:
                    ae.send_alert_notification = old_notify
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())

    def test_create_one_alert_multi_auto_condition(self):
        async def go():
            import database as db
            from telegram_bot import _create_one_alert
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                # Current price: 76946. Targets: 76000 (below), 75000 (below), 79000 (above)
                aid1, desc1 = await _create_one_alert("BTCUSDT", 76946.0, "auto", "76000", False, False, False, False, None, None, single_coin_multi=True)
                aid2, desc2 = await _create_one_alert("BTCUSDT", 76946.0, "auto", "75000", False, False, False, False, None, None, single_coin_multi=True)
                aid3, desc3 = await _create_one_alert("BTCUSDT", 76946.0, "auto", "79000", False, False, False, False, None, None, single_coin_multi=True)

                self.assertIn("below", desc1)
                self.assertIn("below", desc2)
                self.assertIn("above", desc3)

                row1 = await db.get_alert(aid1)
                row2 = await db.get_alert(aid2)
                row3 = await db.get_alert(aid3)

                self.assertEqual(row1[3], "below")
                self.assertEqual(row2[3], "below")
                self.assertEqual(row3[3], "above")
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())



class TestNewFeatures(unittest.TestCase):
    def test_parse_duration(self):
        from telegram_bot import _parse_duration
        self.assertEqual(_parse_duration("15m"), 900)
        self.assertEqual(_parse_duration("2h"), 7200)
        self.assertEqual(_parse_duration("7d"), 604800)
        self.assertEqual(_parse_duration("90"), 90)
        self.assertIsNone(_parse_duration("xyz"))

    def test_parse_expiry(self):
        from telegram_bot import _parse_expiry
        self.assertTrue(_parse_expiry("7d"))
        self.assertTrue(_parse_expiry("12h"))
        self.assertIsNone(_parse_expiry(""))
        self.assertEqual(_parse_expiry("garbage"), "invalid")

    def test_parse_symbols(self):
        from database import parse_symbols
        self.assertEqual(parse_symbols("btc, ETH, solusdt"), ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        self.assertEqual(parse_symbols("btc,btc"), ["BTCUSDT"])

    def test_expired_pruned(self):
        async def go():
            import database as db
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                await db.add_alert("BTCUSDT", 100000, "above", True, expires_at="2000-01-01 00:00:00")
                await db.add_alert("ETHUSDT", 5000, "above", True, expires_at=db.iso_in(days=30))
                removed = await db.prune_expired()
                self.assertEqual(removed, 1)
                self.assertEqual(await db.count_alerts(), 1)
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())

    def test_snooze_suppresses(self):
        async def go():
            import database as db
            from alert_engine import AlertEngine
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                engine = AlertEngine()
                sent = []

                async def fake_notify(**kwargs):
                    sent.append(kwargs)

                import alert_engine as ae
                import datetime as _dt
                old_notify = ae.send_alert_notification
                ae.send_alert_notification = fake_notify
                try:
                    aid = await db.add_alert("BTCUSDT", 70000, "above", False)
                    until = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1)).strftime(
                        "%Y-%m-%d %H:%M:%S")
                    await db.set_snooze(aid, until)
                    await engine.on_price_update("BTCUSDT", 71000)
                    self.assertEqual(len(sent), 0)  # snoozed
                finally:
                    ae.send_alert_notification = old_notify
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())

    def test_trail_fires_on_pullback(self):
        async def go():
            import database as db
            from alert_engine import AlertEngine
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                engine = AlertEngine()
                sent = []

                async def fake_notify(**kwargs):
                    sent.append(kwargs)

                import alert_engine as ae
                old_notify = ae.send_alert_notification
                ae.send_alert_notification = fake_notify
                try:
                    await db.add_alert("BTCUSDT", 100000, "below", True,
                                       alert_type="trail", pct=5.0, base_price=100000, peak_price=100000)
                    await engine.on_price_update("BTCUSDT", 100000)  # no peak yet
                    # price keeps going up -> peak updates, no fire
                    await engine.on_price_update("BTCUSDT", 109000)
                    self.assertEqual(len(sent), 0)
                    # big drop from peak 109000 -> 103500 = -5.05% pullback -> fire
                    await engine.on_price_update("BTCUSDT", 103500)
                    self.assertEqual(len(sent), 1)
                    # persistent: row stays, rebased
                    self.assertEqual(await db.count_alerts(), 1)
                finally:
                    ae.send_alert_notification = old_notify
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())

    def test_watchlist_roundtrip(self):
        async def go():
            import database as db
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                self.assertTrue(await db.add_watch("DOGE"))
                self.assertFalse(await db.add_watch("doge"))  # already present
                self.assertIn("DOGEUSDT", await db.get_watchlist())
                self.assertTrue(await db.remove_watch("DOGE"))
                self.assertNotIn("DOGEUSDT", await db.get_watchlist())
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())

    def test_quiet_hours(self):
        import config as cfg
        self.assertFalse(cfg.quiet_now())  # default off


class TestPricesCache(unittest.TestCase):
    def test_ticker_helpers(self):
        import prices
        ticker = {"lastPrice": "123.45", "price24hPcnt": "0.0123"}
        self.assertEqual(prices.ticker_price(ticker), 123.45)
        self.assertAlmostEqual(prices.ticker_change_24h(ticker), 0.0123)
        self.assertIsNone(prices.ticker_price(None))
        self.assertIsNone(prices.ticker_change_24h({"lastPrice": "x"}))

    def test_cached_ticker_no_network(self):
        import prices
        # Manually seed cache — no network needed.
        prices._cache["TESTUSDT"] = ({"lastPrice": "9.99"}, 1e18)  # ancient ts
        # TTL may be expired, but cached_ticker still returns last-known.
        ticker = prices.cached_ticker("TESTUSDT")
        self.assertIsNotNone(ticker)
        self.assertEqual(prices.ticker_price(ticker), 9.99)
        self.assertIsNone(prices.cached_ticker("NOPEUSDT"))

    def test_prune_cache(self):
        import prices
        # Fill cache with 550 dummy entries
        for i in range(550):
            prices._cache[f"COIN{i}USDT"] = ({"lastPrice": str(i)}, 1.0)
        self.assertGreater(len(prices._cache), 500)
        prices._prune_cache()
        self.assertLessEqual(len(prices._cache), 500)


class TestWebSocketGuards(unittest.TestCase):
    def test_subscribe_unsubscribe(self):
        async def go():
            from binance_ws import BybitWebSocket
            ws = BybitWebSocket(on_price_update=lambda s, p: None)
            self.assertTrue(await ws.subscribe("btcusdt"))
            self.assertIn("BTCUSDT", ws.subscribed_symbols)
            self.assertTrue(await ws.subscribe("BTCUSDT"))
            await ws.unsubscribe("BTCUSDT")
            self.assertNotIn("BTCUSDT", ws.subscribed_symbols)
        _run(go())


if __name__ == "__main__":
    unittest.main()

