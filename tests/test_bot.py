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

    def test_get_fresh_price_staleness(self):
        async def go():
            from alert_engine import AlertEngine
            import datetime
            engine = AlertEngine()
            await engine.on_price_update("SOLUSDT", 98.50)
            self.assertEqual(engine.get_fresh_price("SOLUSDT", max_age_sec=30), 98.50)

            # Manually age the update timestamp to simulate stale cache
            engine.last_update_at["SOLUSDT"] = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=60)
            self.assertIsNone(engine.get_fresh_price("SOLUSDT", max_age_sec=30))
            self.assertEqual(engine.get_fresh_price("SOLUSDT", max_age_sec=120), 98.50)
            self.assertIsNone(engine.get_fresh_price("NONEXISTENT"))
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

    def test_in_memory_expired_alert_skipped(self):
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
                    # Alert that expired in the past
                    await db.add_alert("BTCUSDT", 70000, "above", False, expires_at="2020-01-01 00:00:00")
                    # Force prune throttle to future so db.prune_expired does not run on this tick
                    engine._last_prune_ts = 1e12
                    await engine.on_price_update("BTCUSDT", 75000)
                    # Should NOT fire because in-memory expires_at check skipped it!
                    self.assertEqual(len(sent), 0)
                finally:
                    ae.send_alert_notification = old_notify
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

    def test_cached_price_ttl_expiry(self):
        import prices
        import time
        # Seed cache with a fresh entry
        prices._cache["FRESH"] = ({"lastPrice": "50.0"}, time.monotonic())
        self.assertEqual(prices.cached_price("FRESH"), 50.0)

        # Seed cache with an ancient entry
        prices._cache["STALE"] = ({"lastPrice": "50.0"}, time.monotonic() - 100)
        self.assertIsNone(prices.cached_price("STALE"))
        self.assertEqual(prices.cached_price("STALE", max_age_sec=200), 50.0)

    def test_coinbase_fallback(self):
        async def go():
            import prices
            from unittest.mock import AsyncMock, MagicMock
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"price": "98.75"}
            mock_client.get = AsyncMock(return_value=mock_resp)

            result = await prices._fetch_from_coinbase(mock_client, "SOLUSDT")
            self.assertIsNotNone(result)
            self.assertEqual(result["lastPrice"], "98.75")
        _run(go())


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

    def test_startup_symbols_combination(self):
        alert_symbols = {"BTCUSDT", "ETHUSDT"}  # get_active_symbols returns a set
        watchlist_symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT")  # tuple
        all_symbols = sorted(set(alert_symbols) | set(watchlist_symbols))
        self.assertEqual(all_symbols, ["BTCUSDT", "ETHUSDT", "HYPEUSDT", "SOLUSDT"])


class TestNewPowerFeatures(unittest.TestCase):
    def test_heartbeat_task(self):
        async def go():
            import config as cfg
            import main
            from unittest.mock import AsyncMock, patch

            self.assertEqual(getattr(cfg, "HEARTBEAT_INTERVAL_SEC", 300), 300)

            # Test when HEALTHCHECK_URL is not set: exits immediately
            with patch.object(cfg, "HEALTHCHECK_URL", ""):
                with patch("main.ping_healthcheck", new_callable=AsyncMock) as mock_ping:
                    await main.heartbeat_task()
                    self.assertFalse(mock_ping.called)

            # Test when HEALTHCHECK_URL is set: sends initial ping immediately
            with patch.object(cfg, "HEALTHCHECK_URL", "https://hc-ping.com/fake-uuid"):
                with patch("main.ping_healthcheck", new_callable=AsyncMock) as mock_ping:
                    with patch("asyncio.sleep", side_effect=asyncio.CancelledError) as mock_sleep:
                        try:
                            await main.heartbeat_task()
                        except asyncio.CancelledError:
                            pass
                        self.assertTrue(mock_ping.called)
                        mock_sleep.assert_called_with(300)
        _run(go())

    def test_notification_quick_action_buttons(self):
        async def go():
            import notifier
            from unittest.mock import AsyncMock, MagicMock
            mock_bot = MagicMock()
            mock_bot.send_message = AsyncMock()
            old_send = notifier.SEND_TELEGRAM_ALERTS
            notifier.SEND_TELEGRAM_ALERTS = True
            try:
                await notifier.send_alert_notification(
                    symbol="BTCUSDT",
                    condition="above",
                    target=80000.0,
                    current_price=80050.0,
                    telegram_bot=mock_bot,
                    chat_id=12345,
                    alert_id=42
                )
                self.assertTrue(mock_bot.send_message.called)
                kwargs = mock_bot.send_message.call_args[1]
                self.assertIn("reply_markup", kwargs)
                self.assertIsNotNone(kwargs["reply_markup"])
                buttons = kwargs["reply_markup"].inline_keyboard
                self.assertEqual(len(buttons), 2)
                self.assertIn("quick_add_BTCUSDT_above_", buttons[0][0].callback_data)
                self.assertIn("quick_add_BTCUSDT_below_", buttons[0][1].callback_data)
                self.assertEqual(buttons[1][0].callback_data, "quick_snooze_42_2h")
                self.assertEqual(buttons[1][1].callback_data, "quick_del_42")
                self.assertEqual(buttons[1][2].callback_data, "chart_BTCUSDT")
            finally:
                notifier.SEND_TELEGRAM_ALERTS = old_send
        _run(go())


class TestUrgentAlerts(unittest.TestCase):
    def test_db_urgent_flag(self):
        async def go():
            import database as db
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                aid = await db.add_alert("BTCUSDT", 70000, "above", is_urgent=True)
                alert = await db.get_alert(aid)
                self.assertEqual(db.alert_field(alert, "is_urgent"), 1)
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())

    def test_urgent_notification_delivery(self):
        async def go():
            import notifier
            from unittest.mock import AsyncMock, MagicMock, patch
            mock_bot = MagicMock()
            mock_bot.send_message = AsyncMock()
            old_send = notifier.SEND_TELEGRAM_ALERTS
            notifier.SEND_TELEGRAM_ALERTS = True
            with patch("notifier.send_ntfy", new_callable=AsyncMock) as mock_ntfy:
                mock_ntfy.return_value = True
                try:
                    await notifier.send_alert_notification(
                        symbol="BTCUSDT",
                        condition="above",
                        target=70000,
                        current_price=70100,
                        telegram_bot=mock_bot,
                        chat_id=123,
                        is_urgent=True
                    )
                    # Verify ntfy params
                    self.assertTrue(mock_ntfy.called)
                    call_kwargs = mock_ntfy.call_args[1]
                    self.assertEqual(call_kwargs.get("priority"), "max")
                    self.assertEqual(call_kwargs.get("sound"), "siren")
                    # Verify telegram message
                    self.assertTrue(mock_bot.send_message.called)
                    tg_text = mock_bot.send_message.call_args[1]["text"]
                    self.assertIn("CRITICAL EMERGENCY ALERT", tg_text)
                finally:
                    notifier.SEND_TELEGRAM_ALERTS = old_send
        _run(go())

    def test_toggle_and_set_urgent(self):
        async def go():
            import database as db
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                aid = await db.add_alert("BTCUSDT", 70000, "above", is_urgent=False)
                alert = await db.get_alert(aid)
                self.assertEqual(db.alert_field(alert, "is_urgent"), 0)

                # Toggle ON
                new_val = await db.toggle_urgent(aid)
                self.assertTrue(new_val)
                alert = await db.get_alert(aid)
                self.assertEqual(db.alert_field(alert, "is_urgent"), 1)

                # Toggle OFF
                new_val = await db.toggle_urgent(aid)
                self.assertFalse(new_val)
                alert = await db.get_alert(aid)
                self.assertEqual(db.alert_field(alert, "is_urgent"), 0)

                # Explicit set_urgent
                res = await db.set_urgent(aid, True)
                self.assertTrue(res)
                alert = await db.get_alert(aid)
                self.assertEqual(db.alert_field(alert, "is_urgent"), 1)

                # Non-existent ID
                self.assertIsNone(await db.toggle_urgent(999999))
                self.assertFalse(await db.set_urgent(999999, True))
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())

    def test_cmd_urgent_shortcut(self):
        async def go():
            from unittest.mock import AsyncMock, MagicMock, patch
            import telegram_bot as tb
            import database as db

            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()

                # Test 1: cmd_urgent with no args shows interactive keyboard
                tb._last_cmd_at.clear()
                update = MagicMock()
                update.message = MagicMock()
                update.message.reply_text = AsyncMock()
                update.effective_user.id = 123
                context = MagicMock()
                context.args = []
                context.bot_data = {"engine": None, "ws": AsyncMock()}

                with patch("telegram_bot.config.TELEGRAM_USER_ID", 123):
                    await tb.cmd_urgent(update, context)

                update.message.reply_text.assert_called_once()
                call_args, call_kwargs = update.message.reply_text.call_args
                self.assertIn("Emergency Siren Alert", call_args[0])
                self.assertIsNotNone(call_kwargs.get("reply_markup"))

                # Test 2: cmd_urgent with args creates urgent alert
                tb._last_cmd_at.clear()
                update2 = MagicMock()
                update2.message = MagicMock()
                update2.message.reply_text = AsyncMock()
                update2.effective_user.id = 123
                context2 = MagicMock()
                context2.args = ["BTC", "68000", "below"]
                mock_ws = AsyncMock()
                mock_ws.subscribe = AsyncMock()
                mock_engine = MagicMock()
                mock_engine.get_fresh_price = MagicMock(return_value=69000.0)
                context2.bot_data = {"engine": mock_engine, "ws": mock_ws}

                with patch("telegram_bot.config.TELEGRAM_USER_ID", 123), \
                     patch("telegram_bot._resolve_symbol", return_value=("BTCUSDT", 69000.0)):
                    await tb.cmd_urgent(update2, context2)

                alerts = await db.get_all_alerts()
                self.assertEqual(len(alerts), 1)
                self.assertEqual(db.alert_field(alerts[0], "symbol"), "BTCUSDT")
                self.assertEqual(db.alert_field(alerts[0], "is_urgent"), 1)
                self.assertIn("[🚨 URGENT]", update2.message.reply_text.call_args[0][0])
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())

    def test_toggle_urgent_callback_and_wizard(self):
        async def go():
            from unittest.mock import AsyncMock, MagicMock, patch
            import telegram_bot as tb
            import database as db

            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                aid = await db.add_alert("BTCUSDT", 70000, "above", is_urgent=False)

                # Test 1: Callback toggle_urgent_{aid} toggles to ON
                update = MagicMock()
                update.callback_query = MagicMock()
                update.callback_query.data = f"toggle_urgent_{aid}"
                update.callback_query.answer = AsyncMock()
                update.callback_query.edit_message_text = AsyncMock()
                update.effective_user.id = 123
                context = MagicMock()
                context.bot_data = {"engine": None}

                with patch("telegram_bot.config.TELEGRAM_USER_ID", 123):
                    await tb.handle_callback(update, context)

                alert = await db.get_alert(aid)
                self.assertEqual(db.alert_field(alert, "is_urgent"), 1)
                update.callback_query.answer.assert_called_with("🚨 Emergency Siren ENABLED")

                # Test 2: Wizard siren type sets awaiting_custom_price and is_urgent flag
                update2 = MagicMock()
                update2.callback_query = MagicMock()
                update2.callback_query.data = "addwiz_type_BTC_siren"
                update2.callback_query.answer = AsyncMock()
                update2.callback_query.edit_message_text = AsyncMock()
                update2.effective_user.id = 123
                context2 = MagicMock()
                context2.user_data = {}
                context2.bot_data = {"engine": None}

                with patch("telegram_bot.config.TELEGRAM_USER_ID", 123):
                    await tb.handle_callback(update2, context2)

                self.assertEqual(context2.user_data.get("awaiting_custom_price"), "BTC")
                self.assertTrue(context2.user_data.get("is_urgent"))
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())


class TestCharts(unittest.TestCase):
    def test_fear_and_greed_mock(self):
        async def go():
            import charts
            from unittest.mock import AsyncMock, patch, MagicMock
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "data": [{"value": "78", "value_classification": "Extreme Greed"}]
            }
            with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
                mock_get.return_value = mock_resp
                res = await charts.get_fear_and_greed()
                self.assertIsNotNone(res)
                self.assertEqual(res["value"], 78)
                self.assertEqual(res["classification"], "Extreme Greed")
        _run(go())

    def test_fetch_klines_mock(self):
        async def go():
            import charts
            from unittest.mock import AsyncMock, patch, MagicMock
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "result": {
                    "list": [
                        ["1700003600000", "98.0", "99.0", "97.5", "98.5", "100", "9850"],
                        ["1700000000000", "97.0", "98.2", "96.5", "98.0", "90", "8820"],
                    ]
                }
            }
            with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
                mock_get.return_value = mock_resp
                candles = await charts.fetch_klines("SOLUSDT", interval="1h", limit=10)
                self.assertIsNotNone(candles)
                self.assertEqual(len(candles), 2)
                self.assertEqual(candles[0]["open"], 97.0)
                self.assertEqual(candles[1]["close"], 98.5)
        _run(go())

    def test_tradingview_urls(self):
        import charts
        embed_url = charts.get_tradingview_embed_url("BTC", "1h")
        self.assertIn("s.tradingview.com/widgetembed/", embed_url)
        self.assertIn("BINANCE%3ABTCUSDT", embed_url)
        self.assertIn("interval=60", embed_url)

        web_url = charts.get_tradingview_web_url("SOL")
        self.assertEqual(web_url, "https://www.tradingview.com/chart/?symbol=BINANCE:SOLUSDT")

    def test_generate_chartimg_mock(self):
        async def go():
            import charts
            from unittest.mock import AsyncMock, patch, MagicMock

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = b"fake_tv_png"
            mock_resp.headers = {"content-type": "image/png"}

            with patch("config.CHART_IMG_API_KEY", "test_key"), patch("charts.config.CHART_IMG_API_KEY", "test_key"):
                with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
                    mock_post.return_value = mock_resp
                    png = await charts.generate_chartimg_image("BTCUSDT", interval="1h")
                    self.assertEqual(png, b"fake_tv_png")
                    call_kwargs = mock_post.call_args[1]
                    self.assertIn("x-api-key", call_kwargs["headers"])
                    self.assertEqual(call_kwargs["json"]["symbol"], "BINANCE:BTCUSDT")
                    self.assertEqual(len(call_kwargs["json"]["studies"]), 3)
        _run(go())

    def test_generate_mplfinance_image(self):
        async def go():
            import charts
            from unittest.mock import AsyncMock, patch

            fake_candles = [
                {"time": "12:00", "ts": 1700000000000 + i * 3600000, "open": 65000 + i * 10,
                 "high": 65100 + i * 10, "low": 64900 + i * 10, "close": 65050 + i * 10, "volume": 100 + i}
                for i in range(35)
            ]

            with patch("charts.fetch_klines", new_callable=AsyncMock) as mock_klines:
                mock_klines.return_value = fake_candles
                png = await charts.generate_mplfinance_image("BTCUSDT", interval="1h")
                self.assertIsNotNone(png)
                self.assertGreater(len(png), 1000)
        _run(go())

    def test_generate_quickchart_image_candle_and_line(self):
        async def go():
            import charts
            from unittest.mock import AsyncMock, patch, MagicMock

            fake_candles = [
                {"time": "12:00", "ts": 1700000000000, "open": 65000.0, "high": 66000.0, "low": 64500.0, "close": 65800.0},
                {"time": "13:00", "ts": 1700003600000, "open": 65800.0, "high": 67000.0, "low": 65500.0, "close": 66500.0},
            ]

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = b"fake_candle_png"

            with patch("charts.fetch_klines", new_callable=AsyncMock) as mock_klines, \
                 patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
                mock_klines.return_value = fake_candles
                mock_post.return_value = mock_resp

                png = await charts.generate_quickchart_image("BTCUSDT", interval="1h", chart_type="candle")
                self.assertEqual(png, b"fake_candle_png")
                post_json = mock_post.call_args[1]["json"]
                self.assertEqual(post_json["version"], "3")
                self.assertEqual(post_json["chart"]["type"], "candlestick")
        _run(go())

    def test_cmd_chart_command(self):
        async def go():
            from unittest.mock import AsyncMock, patch, MagicMock
            import telegram_bot as tb

            tb._last_cmd_at.clear()
            update = MagicMock()
            update.message = MagicMock()
            update.message.reply_photo = AsyncMock()
            update.message.reply_text = AsyncMock()
            update.effective_user.id = 123
            update.effective_chat.id = 123
            context = MagicMock()
            context.args = ["BTC", "4h", "line"]
            context.bot = MagicMock()
            context.bot.send_chat_action = AsyncMock()

            with patch("telegram_bot.config.TELEGRAM_USER_ID", 123), \
                 patch("charts.generate_chart_image", new_callable=AsyncMock) as mock_gen:
                mock_gen.return_value = b"fake_chart_bytes"
                await tb.cmd_chart(update, context)

                mock_gen.assert_called_once_with("BTCUSDT", interval="4h", chart_type="line")
                update.message.reply_photo.assert_called_once()
                kwargs = update.message.reply_photo.call_args[1]
                self.assertEqual(kwargs["photo"], b"fake_chart_bytes")
                self.assertIn("Line", kwargs["caption"])
                kb = kwargs["reply_markup"].inline_keyboard
                self.assertEqual(len(kb), 3)
                # Row 1: timeframes
                self.assertEqual(len(kb[0]), 4)
                # Row 2: styles (TV Snap, Tech TA, Clean, Line)
                self.assertEqual(len(kb[1]), 4)
                self.assertIn("chart_BTCUSDT_4h_tv", kb[1][0].callback_data)
                self.assertIn("chart_BTCUSDT_4h_ta", kb[1][1].callback_data)
                # Row 3: WebApp and External link
                self.assertIsNotNone(kb[2][0].web_app)
                self.assertIn("s.tradingview.com/widgetembed/", kb[2][0].web_app.url)
        _run(go())

    def test_chart_callback_inplace_edit(self):
        async def go():
            from unittest.mock import AsyncMock, patch, MagicMock
            import telegram_bot as tb

            update = MagicMock()
            update.callback_query = MagicMock()
            update.callback_query.data = "chart_BTCUSDT_4h_ta"
            update.callback_query.answer = AsyncMock()
            update.callback_query.message = MagicMock()
            update.callback_query.message.photo = [MagicMock()]  # message already has a photo
            update.callback_query.edit_message_media = AsyncMock()
            update.effective_user.id = 123
            context = MagicMock()
            context.bot_data = {"engine": None}

            with patch("telegram_bot.config.TELEGRAM_USER_ID", 123), \
                 patch("charts.generate_chart_image", new_callable=AsyncMock) as mock_gen:
                mock_gen.return_value = b"updated_chart_bytes"
                await tb.handle_callback(update, context)

                mock_gen.assert_called_once_with("BTCUSDT", interval="4h", chart_type="ta")
                update.callback_query.edit_message_media.assert_called_once()
                call_kwargs = update.callback_query.edit_message_media.call_args[1]
                self.assertIn("reply_markup", call_kwargs)
                self.assertEqual(len(call_kwargs["reply_markup"].inline_keyboard), 3)
        _run(go())


class TestWebhookServer(unittest.TestCase):
    def test_http_request_parsing(self):
        from webhook_server import _parse_http_request
        raw = b"POST /webhook/secret123 HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n\r\n{\"symbol\":\"SOL\",\"price\":150}"
        method, path, headers, body = _parse_http_request(raw)
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/webhook/secret123")
        self.assertEqual(headers.get("content-type"), "application/json")
        self.assertIn("\"symbol\":\"SOL\"", body)

    def test_webhook_server_endpoints(self):
        async def go():
            import webhook_server
            import config
            import httpx
            from unittest.mock import AsyncMock, patch, MagicMock

            old_secret = config.WEBHOOK_SECRET
            old_port = config.WEBHOOK_PORT
            test_port = 19123
            config.WEBHOOK_SECRET = "supersecret"
            config.WEBHOOK_PORT = test_port

            mock_bot = MagicMock()
            mock_bot.send_message = AsyncMock()

            server_task = asyncio.create_task(webhook_server.run_webhook_server(telegram_bot=mock_bot))
            await asyncio.sleep(0.1)

            try:
                async with httpx.AsyncClient(trust_env=False, timeout=3.0) as client:
                    # 1. Health ping
                    r = await client.get(f"http://127.0.0.1:{test_port}/health")
                    self.assertEqual(r.status_code, 200)
                    self.assertEqual(r.json(), {"status": "ok"})

                    # 2. Unauthorized webhook (no secret)
                    r = await client.post(f"http://127.0.0.1:{test_port}/webhook", json={"test": 1})
                    self.assertEqual(r.status_code, 401)

                    # 3. Authorized webhook via URL path
                    with patch("webhook_server.send_ntfy", new_callable=AsyncMock) as mock_ntfy:
                        mock_ntfy.return_value = True
                        r = await client.post(
                            f"http://127.0.0.1:{test_port}/webhook/supersecret",
                            json={"symbol": "BTC", "action": "BUY", "price": "75000", "message": "Golden Cross"}
                        )
                        self.assertEqual(r.status_code, 200)
                        self.assertEqual(r.json().get("status"), "ok")
                        self.assertTrue(mock_ntfy.called)
            finally:
                webhook_server.stop_webhook_server()
                server_task.cancel()
                try:
                    await server_task
                except asyncio.CancelledError:
                    pass
                config.WEBHOOK_SECRET = old_secret
                config.WEBHOOK_PORT = old_port

        _run(go())

    def test_export_import_is_urgent(self):
        async def go():
            import database as db
            import json
            from telegram_bot import cmd_export, cmd_import
            from unittest.mock import AsyncMock, MagicMock

            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                aid = await db.add_alert("BTCUSDT", 70000, "above", is_urgent=True)
                alerts = await db.get_all_alerts()
                cols = ["id", "symbol", "target", "condition", "created_at", "is_persistent",
                        "last_triggered_at", "alert_type", "expires_at", "snoozed_until",
                        "cooldown_sec", "pct", "window_min", "base_price", "peak_price", "funding_rate", "is_urgent"]
                exported_data = [dict(zip(cols, list(a) + [None] * (len(cols) - len(a)))) for a in alerts]
                self.assertEqual(exported_data[0]["is_urgent"], 1)

                # Simulate import
                await db.remove_alert(aid)
                self.assertEqual(await db.count_alerts(), 0)

                item = exported_data[0]
                new_aid = await db.add_alert(
                    item["symbol"], item["target"], item["condition"], bool(item["is_persistent"]),
                    is_urgent=bool(item.get("is_urgent", 0))
                )
                imported_alert = await db.get_alert(new_aid)
                self.assertEqual(db.alert_field(imported_alert, "is_urgent"), 1)
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())


class TestUIRedesign(unittest.TestCase):
    def test_main_keyboard_streamlined(self):
        from telegram_bot import get_main_keyboard
        kb = get_main_keyboard()
        buttons = [btn.text for row in kb.keyboard for btn in row]
        self.assertEqual(len(buttons), 4)
        self.assertIn("⚡ Dashboard", buttons)
        self.assertIn("➕ Set Alert", buttons)
        self.assertIn("💰 Prices", buttons)
        self.assertIn("📋 My Alerts", buttons)

    def test_database_toggles(self):
        async def go():
            import database as db
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                aid = await db.add_alert("BTCUSDT", 70000, "above", is_persistent=False)
                # Test toggle_persistent
                new_val = await db.toggle_persistent(aid)
                self.assertTrue(new_val)
                alert = await db.get_alert(aid)
                self.assertEqual(db.alert_field(alert, "is_persistent"), 1)

                new_val2 = await db.toggle_persistent(aid)
                self.assertFalse(new_val2)
                alert2 = await db.get_alert(aid)
                self.assertEqual(db.alert_field(alert2, "is_persistent"), 0)

                # Test watchlist toggles
                self.assertFalse(await db.is_watched("SOLUSDT"))
                self.assertTrue(await db.toggle_watch("SOLUSDT"))
                self.assertTrue(await db.is_watched("SOLUSDT"))
                self.assertFalse(await db.toggle_watch("SOLUSDT"))
                self.assertFalse(await db.is_watched("SOLUSDT"))
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())

    def test_render_decks_and_cards(self):
        async def go():
            import database as db
            from telegram_bot import (
                _render_dashboard, _show_coin_card, _render_movers_deck,
                _render_watch_deck, _render_pause_deck, _render_tools_deck,
                _render_charts_hub, _render_wiz_start, _render_wiz_coin,
                _render_alert_editor
            )
            from unittest.mock import AsyncMock, MagicMock, patch

            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                aid = await db.add_alert("SOLUSDT", 150.0, "above", is_persistent=True, is_urgent=True)
                mock_engine = MagicMock()
                mock_engine.is_muted.return_value = False
                mock_engine.get_stats.return_value = {"checks": 10, "triggered": 1}
                mock_engine.get_fresh_price.return_value = 145.0

                # 1. Dashboard
                text, markup = await _render_dashboard(mock_engine)
                self.assertIn("CRYPTO COMMAND CENTER", text)
                cb_data = [btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data]
                self.assertIn("wiz_start", cb_data)
                self.assertIn("hub_alerts", cb_data)
                self.assertIn("hub_movers", cb_data)
                self.assertIn("hub_charts", cb_data)
                self.assertIn("hub_watch", cb_data)
                self.assertIn("hub_tools", cb_data)
                self.assertIn("hub_refresh", cb_data)

                # 2. Coin Action Card
                mock_target = AsyncMock()
                await _show_coin_card(mock_target, "SOL", mock_engine)
                self.assertTrue(mock_target.reply_text.called)
                card_text = mock_target.reply_text.call_args[0][0]
                card_markup = mock_target.reply_text.call_args[1]["reply_markup"]
                self.assertIn("SOL / USDT", card_text)
                card_cbs = [btn.callback_data for row in card_markup.inline_keyboard for btn in row if btn.callback_data]
                self.assertIn("chart_SOLUSDT_1h_tv", card_cbs)
                self.assertIn("wiz_coin_SOL", card_cbs)
                self.assertIn("wiz_type_SOL_siren", card_cbs)
                self.assertIn("wiz_grid_SOL", card_cbs)
                self.assertIn("watch_toggle_SOL", card_cbs)
                self.assertIn("hub_main", card_cbs)

                # 3. Alert Editor Deck
                res = await _render_alert_editor(aid)
                self.assertIsNotNone(res)
                ed_text, ed_markup = res
                self.assertIn(f"Manage Alert #{aid}", ed_text)
                self.assertIn("SOL", ed_text)
                ed_cbs = [btn.callback_data for row in ed_markup.inline_keyboard for btn in row if btn.callback_data]
                self.assertIn(f"edittgt_{aid}_1", ed_cbs)
                self.assertIn(f"edittgt_{aid}_5", ed_cbs)
                self.assertIn(f"edittgt_{aid}_-1", ed_cbs)
                self.assertIn(f"edittgt_{aid}_-5", ed_cbs)
                self.assertIn(f"edit_flip_{aid}", ed_cbs)
                self.assertIn(f"edit_custom_{aid}", ed_cbs)
                self.assertIn(f"toggle_urgent_{aid}", ed_cbs)
                self.assertIn(f"toggle_repeat_{aid}", ed_cbs)
                self.assertIn(f"snooze_{aid}_1h", ed_cbs)
                self.assertIn(f"remove_{aid}", ed_cbs)
                self.assertIn("hub_alerts", ed_cbs)

                # 4. Watchlist Deck
                w_text, w_markup = await _render_watch_deck(mock_engine)
                self.assertIn("Watchlist Deck", w_text)

                # 5. Pause Deck
                p_text, p_markup = await _render_pause_deck(mock_engine)
                self.assertIn("Pause / Resume", p_text)

                # 6. Tools Deck
                t_text, t_markup = await _render_tools_deck(mock_engine, None)
                self.assertIn("Tools & Diagnostics", t_text)

                # 7. Wizard Start & Coin
                wiz_text, wiz_markup = await _render_wiz_start()
                self.assertIn("Step 1: Select a Coin", wiz_text)

                coin_text, coin_markup = await _render_wiz_coin("BTC", mock_engine)
                self.assertIn("Set Alert for BTC", coin_text)
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())


class TestAuditRemediations(unittest.TestCase):
    def test_iso_in_seconds(self):
        import database as db
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        result = db.iso_in(seconds=120)
        parsed = datetime.datetime.strptime(result, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
        diff = (parsed - now).total_seconds()
        self.assertTrue(115 <= diff <= 125)

    def test_should_keep_subscribed(self):
        async def go():
            import database as db
            import config
            from telegram_bot import _should_keep_subscribed
            from unittest.mock import patch
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                with patch.object(config, "WATCHLIST_SYMBOLS", ()):
                    # Symbol with alert
                    aid = await db.add_alert("BTCUSDT", 70000, "above")
                    self.assertTrue(await _should_keep_subscribed("BTCUSDT"))

                    # Symbol without alert, not watched
                    self.assertFalse(await _should_keep_subscribed("ETHUSDT"))

                    # Now watch ETHUSDT
                    await db.add_watch("ETHUSDT")
                    self.assertTrue(await _should_keep_subscribed("ETHUSDT"))

                    # Remove alert for BTCUSDT, should now be False (not watched)
                    await db.remove_alert(aid)
                    self.assertFalse(await _should_keep_subscribed("BTCUSDT"))
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())

    def test_alert_editor_alert_type_separation(self):
        async def go():
            import database as db
            from telegram_bot import _render_alert_editor
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                # 1. Price alert
                aid_price = await db.add_alert("BTCUSDT", 70000, "above", alert_type="price")
                res_price = await _render_alert_editor(aid_price)
                self.assertIsNotNone(res_price)
                txt_p, kb_p = res_price
                cb_data_p = [btn.callback_data for row in kb_p.inline_keyboard for btn in row]
                self.assertIn(f"edittgt_{aid_price}_1", cb_data_p)
                self.assertIn(f"edit_flip_{aid_price}", cb_data_p)
                self.assertIn(f"edit_custom_{aid_price}", cb_data_p)

                # 2. Trailing stop alert
                aid_trail = await db.add_alert("ETHUSDT", 3000, "below", alert_type="trail", pct=5.0)
                res_trail = await _render_alert_editor(aid_trail)
                self.assertIsNotNone(res_trail)
                txt_t, kb_t = res_trail
                self.assertIn("Trailing Stop", txt_t)
                cb_data_t = [btn.callback_data for row in kb_t.inline_keyboard for btn in row]
                self.assertNotIn(f"edittgt_{aid_trail}_1", cb_data_t)
                self.assertNotIn(f"edit_flip_{aid_trail}", cb_data_t)
                self.assertNotIn(f"edit_custom_{aid_trail}", cb_data_t)
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())

    def test_tools_deck_callback_handling(self):
        async def go():
            import database as db
            from telegram_bot import cmd_history, cmd_health
            from unittest.mock import AsyncMock, MagicMock, patch
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            old_path, old_conn = db.DB_PATH, db._db
            db.DB_PATH, db._db = tmp.name, None
            try:
                await db.init_db()
                # Mock update representing a callback query: update.message is None!
                mock_update = MagicMock()
                mock_update.message = None
                mock_update.effective_user.id = 123
                mock_cb_msg = MagicMock()
                mock_cb_msg.reply_text = AsyncMock()
                mock_update.callback_query.message = mock_cb_msg

                mock_context = MagicMock()
                mock_context.args = []
                mock_context.bot_data = {
                    "engine": MagicMock(get_stats=MagicMock(return_value={}), last_update_at={}),
                    "ws": MagicMock(connected=True, reconnects=0, subscribed_symbols=set())
                }

                with patch("telegram_bot.config.TELEGRAM_USER_ID", 123):
                    # cmd_history with update.message=None should not crash
                    await cmd_history(mock_update, mock_context)
                    self.assertTrue(mock_cb_msg.reply_text.called)

                    # cmd_health with update.message=None should not crash
                    mock_cb_msg.reply_text.reset_mock()
                    await cmd_health(mock_update, mock_context)
                    self.assertTrue(mock_cb_msg.reply_text.called)
            finally:
                await db.close_db()
                db.DB_PATH, db._db = old_path, old_conn
                os.unlink(tmp.name)
        _run(go())


if __name__ == "__main__":
    unittest.main()

