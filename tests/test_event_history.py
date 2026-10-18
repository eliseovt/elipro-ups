import base64
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from ups_monitor.core import DEFAULT_POLICY, Store, iso_utc, utc_now
from ups_monitor.persistence import HistoryUnavailable, ResilientStore


TEST_ROOT = Path(__file__).resolve().parents[1] / ".test-data"


class EventHistoryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.base = utc_now() - timedelta(hours=1)

    def tearDown(self):
        self.store.close()

    def stamp(self, seconds):
        return iso_utc(self.base + timedelta(seconds=seconds))

    def event(self, kind, seconds, message=None, data=None):
        return self.store.add_event(kind, message or kind, data, timestamp=self.stamp(seconds))

    def test_cursor_excludes_new_insertions_without_duplicates(self):
        original = [self.event("power_lost", second) for second in range(7)]
        page = self.store.events_page(limit=2)
        seen = page["events"]
        self.event("new", 20)
        while page["next_cursor"]:
            page = self.store.events_page(limit=2, cursor=page["next_cursor"])
            self.assertEqual(page["total"], 7)
            seen.extend(page["events"])
        self.assertEqual([item["id"] for item in seen], [item["id"] for item in reversed(original)])

    def test_filters_are_inclusive_and_search_is_literal(self):
        self.event("power_lost", 0, "Corte 100%_seguro: batería", {"reason": "red"})
        self.event("power_restored", 10, "Regresó la energía")
        self.event("power_lost", 20, "Otro corte")
        page = self.store.events_page(start=self.stamp(0), end=self.stamp(10), query="100%_")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["events"][0]["kind"], "power_lost")
        self.assertEqual(self.store.events_page(query="batería")["total"], 1)
        self.assertEqual(self.store.events_page(query="' OR 1=1 --")["total"], 0)
        self.assertEqual(self.store.events_page(kind="power_lost")["total"], 2)
        self.assertEqual(self.store.events_page(start=self.stamp(10), end=self.stamp(10))["total"], 1)

    def test_cursor_bound_to_filters_and_not_sql(self):
        for second in range(3):
            self.event("power_lost", second)
        cursor = self.store.events_page(limit=1, kind="power_lost")["next_cursor"]
        for bad in ("bad", "' OR 1=1", "A" * 513,
                    base64.urlsafe_b64encode(json.dumps({"v": 1}).encode()).decode()):
            with self.assertRaises(ValueError):
                self.store.events_page(cursor=bad)
        with self.assertRaises(ValueError):
            self.store.events_page(kind="power_restored", cursor=cursor)
        self.assertEqual(self.store.events_page()["total"], 3)

    def test_invalid_ranges_types_and_limits(self):
        for arguments in ({"start": "2026-10-17"}, {"end": "bad"},
                          {"start": self.stamp(2), "end": self.stamp(1)},
                          {"limit": True}, {"limit": 201}, {"limit": 0},
                          {"kind": 1}, {"query": "x" * 201}):
            with self.assertRaises(ValueError):
                self.store.events_page(**arguments)
        with self.assertRaises(ValueError):
            list(self.store.iter_events(start="bad"))

    def test_timezones_normalize_to_utc(self):
        self.store.add_event("power_lost", "Lima", timestamp="2026-10-17T12:30:00-05:00")
        page = self.store.events_page(start="2026-10-17T17:30:00Z", end="2026-10-17T17:30:00+00:00")
        self.assertEqual(page["events"][0]["timestamp"], "2026-10-17T17:30:00Z")

    def test_iter_events_complete_and_freezes_maximum_id(self):
        for second in range(1003):
            self.event("power_lost", second, "batería, \"UPS\"\nenergía", {"a": "á"})
        iterator = self.store.iter_events()
        first = next(iterator)
        self.event("later", 2000)
        rows = [first, *iterator]
        self.assertEqual(len(rows), 1003)
        self.assertEqual(rows[-1]["id"], 1003)
        self.assertEqual(rows[0]["data"], {"a": "á"})

    def test_mixed_power_communication_and_actions_are_independent(self):
        power = self.event("power_lost", 0, "Corte")
        self.event("policy_countdown", 2)
        communication = self.event("connection_lost", 3, "USB desconectado", {"error_kind": "usb"})
        self.event("monitor_error", 4, "El lector falló", {"error_kind": "backend"})
        self.event("policy_unknown", 5)
        self.event("policy_cancelled", 6)
        self.event("connection_restored", 7)
        self.event("policy_action_started", 8)
        self.event("policy_action_failed", 9)
        self.event("power_restored", 10)
        rows = {item["type"]: item for item in self.store.incidents()["incidents"]}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows["power"]["opening_event_id"], power["id"])
        self.assertEqual(rows["power"]["duration_seconds"], 10)
        self.assertEqual(rows["power"]["action_result"], "failed")
        self.assertEqual(rows["communication"]["opening_event_id"], communication["id"])
        self.assertEqual(rows["communication"]["duration_seconds"], 4)
        self.assertEqual(rows["communication"]["action_result"], "cancelled")
        self.assertIn("policy_unknown", [item["kind"] for item in rows["communication"]["actions"]])

    def test_incident_overlap_includes_opening_before_filter(self):
        self.event("power_lost", 0)
        self.event("power_restored", 20)
        self.event("connection_lost", 30)
        self.assertEqual(self.store.incidents(start=self.stamp(10), end=self.stamp(15))["total"], 1)
        self.assertEqual(self.store.incidents(start=self.stamp(25), end=self.stamp(28))["total"], 0)
        page = self.store.incidents(start=self.stamp(40), end=self.stamp(50))
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["incidents"][0]["status"], "open")
        self.assertEqual(page["incidents"][0]["started_at"], self.stamp(30))

    def test_stopped_monitor_or_orphan_recovery_do_not_invent_incidents(self):
        self.event("connection_lost", 0, data={"error_kind": "stopped"})
        self.event("connection_restored", 1)
        self.event("power_restored", 2)
        self.assertEqual(self.store.incidents()["total"], 0)

    def test_incident_cursor_freezes_recovery_and_new_incidents(self):
        for second in (0, 10, 20):
            self.event("power_lost", second)
            self.event("power_restored", second + 1)
        first = self.store.incidents(limit=1)
        self.event("power_lost", 30)
        second = self.store.incidents(limit=1, cursor=first["next_cursor"])
        third = self.store.incidents(limit=1, cursor=second["next_cursor"])
        self.assertEqual(second["total"], 3)
        self.assertIsNone(third["next_cursor"])
        self.assertEqual(len({first["incidents"][0]["id"], second["incidents"][0]["id"], third["incidents"][0]["id"]}), 3)

    def test_corrupted_event_payload_is_not_silently_discarded(self):
        self.event("power_lost", 0)
        self.store._db.execute("UPDATE events SET data='broken'")
        self.store._db.commit()
        with self.assertRaises(json.JSONDecodeError):
            self.store.events_page()
        with self.assertRaises(json.JSONDecodeError):
            self.store.incidents()

    def test_backup_includes_wal_settings_and_preserves_source(self):
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            source, backup = Path(directory) / "source.sqlite3", Path(directory) / "backup.sqlite3"
            store = Store(source)
            try:
                store.add_event("power_lost", "Corte")
                store.add_sample({"timestamp": iso_utc(), "on_battery": True})
                store.set_setting("policy", DEFAULT_POLICY)
                metadata = store.backup(backup)
                self.assertEqual((metadata["event_count"], metadata["sample_count"], metadata["settings_count"]), (1, 1, 1))
                with closing(sqlite3.connect(backup)) as db:
                    self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    policy = json.loads(db.execute("SELECT value FROM settings WHERE key='policy'").fetchone()[0])
                    self.assertFalse(policy["enabled"])
                store.add_event("power_restored", "Regresó")
                with closing(sqlite3.connect(backup)) as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
                with self.assertRaises(ValueError):
                    store.backup(source)
                with self.assertRaises(ValueError):
                    store.backup(backup)
                with self.assertRaises(sqlite3.OperationalError):
                    store.backup(Path(directory) / "missing" / "backup.sqlite3")
            finally:
                store.close()


class ResilientEventHistoryTests(unittest.TestCase):
    def setUp(self):
        self.backend = Store(":memory:", busy_timeout_ms=100)
        self.store = ResilientStore.open(":memory:", store_factory=lambda path: self.backend,
                                         retry_seconds=0.01, max_retry_seconds=0.02)

    def tearDown(self):
        self.store.close(timeout=1)

    def test_invalid_arguments_are_not_storage_failures(self):
        for operation in (lambda: self.store.events_page(limit=0),
                          lambda: self.store.events_page(cursor="bad"),
                          lambda: self.store.incidents(start="bad"),
                          lambda: self.store.iter_events(query="x" * 201)):
            with self.assertRaises(ValueError):
                operation()
        self.assertTrue(self.store.status()["available"])

    def test_failed_new_reads_keep_memory_cache_and_recover(self):
        self.store.add_event("power_lost", "Corte")
        with patch.object(self.backend, "events_page", side_effect=sqlite3.DatabaseError("malformed")):
            with self.assertRaises(HistoryUnavailable):
                self.store.events_page()
        self.assertFalse(self.store.status()["available"])
        self.assertEqual(self.store.events()[0]["kind"], "power_lost")
        self.store.events_page()
        self.assertTrue(self.store.status()["available"])

    def test_export_releases_gate_between_500_row_batches(self):
        for number in range(501):
            self.backend.add_event("test", str(number))
        iterator = self.store.iter_events()
        self.assertEqual(next(iterator)["message"], "0")
        self.store.set_setting("test", {"ok": True})
        self.assertEqual(len(list(iterator)), 500)

    def test_backup_failure_and_slow_backup_cannot_block_telemetry_queue(self):
        entered, release = threading.Event(), threading.Event()
        def slow_backup(destination):
            entered.set()
            release.wait(2)
            raise sqlite3.OperationalError("database or disk is full")
        self.backend.backup = slow_backup
        errors = []
        def request():
            try:
                self.store.backup("unused")
            except HistoryUnavailable as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=request)
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            began = time.monotonic()
            self.store.add_sample({"timestamp": iso_utc(), "on_battery": True})
            self.store.add_event("power_lost", "Corte")
            self.assertFalse(self.store.get_setting("policy")["enabled"])
            self.assertLess(time.monotonic() - began, 0.1)
            self.assertEqual(self.store.status()["pending"], 2)
        finally:
            release.set()
            thread.join(1)
        self.assertIn("disk is full", errors[0])


if __name__ == "__main__":
    unittest.main()
