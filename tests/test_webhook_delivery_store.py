import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ups_monitor.core import Store, iso_utc
from ups_monitor.persistence import ResilientStore


def delivery(number=0, **changes):
    return {"id": f"delivery-{number:03d}", "timestamp": iso_utc(datetime.now(timezone.utc) + timedelta(seconds=number)),
            "event": "power_lost", "state": "success", "attempts": 1, "max_attempts": 2,
            "message": "Entregado.", "http_status": 204, **changes}


class WebhookDeliveryStoreTests(unittest.TestCase):
    def test_delivery_updates_one_row_without_creating_ups_events(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        first = delivery(state="retrying")
        store.add_webhook_delivery(first)
        updated = {**first, "state": "success", "attempts": 2}
        store.add_webhook_delivery(updated)
        updated["message"] = "Mutated later"
        rows = store.webhook_deliveries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempts"], 2)
        self.assertEqual(rows[0]["message"], "Entregado.")
        self.assertEqual(store.events(), [])

    def test_only_latest_fifty_deliveries_survive_database_reopen(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "history.sqlite3"
            store = Store(path)
            for number in range(55):
                store.add_webhook_delivery(delivery(number))
            self.assertEqual([row["id"] for row in store.webhook_deliveries(limit=2)], ["delivery-054", "delivery-053"])
            store.close()
            store = Store(path)
            try:
                self.assertEqual(len(store.webhook_deliveries()), 50)
                self.assertEqual(store.webhook_deliveries()[-1]["id"], "delivery-005")
                for limit in (0, 51, True, "5"):
                    with self.subTest(limit=limit), self.assertRaises(ValueError):
                        store.webhook_deliveries(limit)
            finally:
                store.close()

    def test_delivery_history_shares_atomic_batch_commit_and_rollback(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        event = {"kind": "power_lost", "message": "Corte", "data": {}, "timestamp": iso_utc()}
        with patch.object(store, "add_event", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.write_batch([("webhook_delivery", delivery()), ("event", event)])
        self.assertEqual(store.webhook_deliveries(), [])
        store.write_batch([("webhook_delivery", delivery()), ("event", event)])
        self.assertEqual(len(store.webhook_deliveries()), 1)
        self.assertEqual(len(store.events()), 1)

    def test_resilient_history_is_primed_before_start_and_reads_stay_in_memory(self):
        backend = Store(":memory:")
        record = delivery()
        backend.add_webhook_delivery(record)
        store = ResilientStore.open(":memory:", store_factory=lambda _path: backend)
        self.addCleanup(store.close)
        store.start()
        with patch.object(backend, "webhook_deliveries", side_effect=AssertionError("No SQLite reads on a tick")):
            self.assertEqual(store.webhook_deliveries(), [record])
            rows = store.webhook_deliveries()
            rows[0]["state"] = "error"
            self.assertEqual(store.webhook_deliveries()[0]["state"], "success")

    def test_stalled_history_never_blocks_delivery_cache_or_status(self):
        backend = Store(":memory:")
        store = ResilientStore.open(":memory:", store_factory=lambda _path: backend)
        entered, release = threading.Event(), threading.Event()
        original = backend.add_webhook_delivery

        def stalled(record):
            entered.set()
            release.wait(2)
            return original(record)

        backend.add_webhook_delivery = stalled
        store.start()
        store.add_webhook_delivery(delivery())
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            for number in range(1, 10):
                store.add_webhook_delivery(delivery(number))
                store.add_event("power_lost", "Corte")
                self.assertTrue(store.webhook_deliveries())
                self.assertIsNotNone(store.status())
            self.assertLess(time.monotonic() - started, 0.2)
        finally:
            release.set()
            self.assertTrue(store.close())


if __name__ == "__main__":
    unittest.main()
