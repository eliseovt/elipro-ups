"""Unified retained register: group evidence before filtering or pagination."""
import base64
import json
import sqlite3
import unittest
from datetime import timedelta
from unittest.mock import patch

from ups_monitor.core import RecordCursorExpired, Store, iso_utc, utc_now
from ups_monitor.persistence import HistoryUnavailable, ResilientStore


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.base = utc_now() - timedelta(hours=1)

    def stamp(self, seconds):
        return iso_utc(self.base + timedelta(seconds=seconds))

    def event(self, kind, seconds, message=None, data=None):
        return self.store.add_event(kind, message or kind, data, timestamp=self.stamp(seconds))

    def test_members_and_shared_actions_are_grouped_once_without_losing_independent_conditions(self):
        opening = self.event("power_lost", 0, "Corte eléctrico")
        repeated = self.event("power_lost", 1, "Corte repetido")
        usb = self.event("connection_lost", 2, "USB desconectado")
        shared = self.event("policy_countdown", 3, "Cuenta de protección")
        independent = [self.event("battery_low", 4), self.event("overload", 5),
                       self.event("ups_fault", 6), self.event("policy_changed", 7),
                       self.event("notifications_changed", 8), self.event("custom", 9)]
        usb_recovery = self.event("connection_restored", 10)
        power_recovery = self.event("power_restored", 11)
        orphan = self.event("power_restored", 12, "Retorno sin inicio conservado")
        stopped = self.event("connection_lost", 13, data={"error_kind": "stopped"})
        page = self.store.records()
        groups = {record["type"]: record for record in page["records"] if record["record_type"] == "incident"}
        self.assertEqual(set(groups), {"power", "connection"})
        self.assertEqual([event["id"] for event in groups["power"]["events"]],
                         [opening["id"], repeated["id"], shared["id"], power_recovery["id"]])
        self.assertEqual([event["id"] for event in groups["connection"]["events"]],
                         [usb["id"], shared["id"], usb_recovery["id"]])
        for group in groups.values():
            self.assertEqual(group["status"], "recovered")
            self.assertEqual(group["actions"], [shared])
            self.assertEqual(group["start_known"], True)
        self.assertEqual(groups["power"]["duration_seconds"], 11)
        standalone = {record["events"][0]["id"] for record in page["records"] if record["record_type"] == "event"}
        self.assertEqual(standalone, {event["id"] for event in independent + [orphan, stopped]})
        self.assertEqual(page["total"], 10)

    def test_search_and_member_categories_return_complete_groups_without_duplicate_events(self):
        opening = self.event("power_lost", 0, "Apertura")
        self.event("connection_lost", 1, "USB")
        action = self.event("policy_cancelled", 2, "Cancelación 100%_literal", {"reason": "BATERÍA roja"})
        self.event("connection_restored", 3)
        recovery = self.event("power_restored", 4, "La energía volvió")
        settings = self.event("policy_changed", 5, "Configuración")
        for filters in ({"query": "100%_literal"}, {"query": "batería roja"}, {"kind": "protection"}):
            with self.subTest(filters=filters):
                records = self.store.records(**filters)["records"]
                self.assertEqual(len(records), 2)
                self.assertTrue(all(record["record_type"] == "incident" for record in records))
                self.assertTrue(all(action in record["events"] for record in records))
                power = next(record for record in records if record["type"] == "power")
                self.assertEqual(power["events"], [opening, action, recovery])
                self.assertEqual(power["category"], "power")
                self.assertEqual(power["action_result"], "cancelled")
        self.assertEqual(self.store.records(query="energía volvió")["records"][0]["events"], [opening, action, recovery])
        self.assertEqual(self.store.records(kind="settings")["records"][0]["events"], [settings])
        self.assertEqual(self.store.records(query="' OR 1=1 --")["total"], 0)

    def test_incidents_overlap_the_interval_while_events_use_exact_inclusive_timestamps(self):
        self.event("power_lost", 0)
        self.event("battery_low", 9)
        edge = self.event("overload", 10)
        self.event("power_restored", 20)
        self.event("connection_lost", 30)
        self.event("ups_fault", 40)
        page = self.store.records(start=self.stamp(10), end=self.stamp(15))
        self.assertEqual(page["total"], 2)
        self.assertEqual({record["record_type"] for record in page["records"]}, {"incident", "event"})
        self.assertEqual(next(record for record in page["records"] if record["record_type"] == "event")["events"], [edge])
        open_page = self.store.records(start=self.stamp(50), end=self.stamp(60))
        self.assertEqual(open_page["total"], 1)
        self.assertEqual(open_page["records"][0]["type"], "connection")
        self.assertEqual(open_page["records"][0]["status"], "open")

    def test_grouping_precedes_limits_and_a_single_cursor_pages_the_entire_feed(self):
        self.event("power_lost", 0)
        for second in range(1, 121):
            self.event("policy_countdown", second)
        self.event("power_restored", 122)
        for second in range(123, 183):
            self.event("custom", second)
        page = self.store.records(limit=2)
        records = list(page["records"])
        while page["next_cursor"]:
            page = self.store.records(limit=2, cursor=page["next_cursor"])
            self.assertEqual(page["total"], 61)
            records.extend(page["records"])
        self.assertEqual(len(records), 61)
        self.assertEqual(len({record["id"] for record in records}), 61)
        self.assertEqual(records[-1]["record_type"], "incident")
        self.assertEqual(len(records[-1]["events"]), 122)

    def test_cursor_freezes_event_high_watermark_open_duration_and_recovery(self):
        opening = self.event("power_lost", 0)
        self.event("custom", 1)
        as_of = self.base + timedelta(seconds=15)
        with patch("ups_monitor.core.utc_now", return_value=as_of):
            first = self.store.records(limit=1)
        self.event("power_restored", 20)
        self.event("custom", -1, "Inserción tardía con fecha anterior")
        with patch("ups_monitor.core.utc_now", return_value=as_of + timedelta(hours=1)):
            second = self.store.records(limit=1, cursor=first["next_cursor"])
        self.assertEqual(second["as_of"], first["as_of"])
        self.assertEqual(second["total"], 2)
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(second["records"][0]["events"], [opening])
        self.assertEqual(second["records"][0]["status"], "open")
        self.assertEqual(second["records"][0]["duration_seconds"], 15)

    def test_dates_sort_before_id_with_stable_ties(self):
        latest = self.event("custom", 20)
        older = self.event("custom", 10)
        same_time = self.event("custom", 20)
        rows = self.store.records()["records"]
        self.assertEqual([record["id"] for record in rows],
                         ["event:" + str(event["id"]) for event in (same_time, latest, older)])

    def test_cursor_is_bound_to_all_filters_and_rejects_invalid_snapshots(self):
        self.event("custom", 0)
        self.event("custom", 1)
        cursor = self.store.records(limit=1)["next_cursor"]
        for options in ({"kind": "other"}, {"query": "custom"}, {"start": self.stamp(0)}, {"end": self.stamp(2)}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.store.records(cursor=cursor, **options)
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        for changes in ({"v": True}, {"high": True}, {"high": -1}, {"before": "event:0"},
                        {"before": "other:1"}, {"as_of": "2026-10-18"}, {"extra": 1}):
            invalid = base64.urlsafe_b64encode(json.dumps({**payload, **changes}).encode()).decode().rstrip("=")
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.records(cursor=invalid)
        with self.assertRaises(ValueError):
            self.store.records(cursor=self.store.events_page(limit=1)["next_cursor"])
        for options in ({"kind": "power_lost"}, {"limit": 201}, {"limit": True}, {"query": "x" * 201},
                        {"start": self.stamp(2), "end": self.stamp(1)}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.store.records(**options)

    def test_expired_anchor_has_its_own_error_without_marking_persistence_unavailable(self):
        self.event("custom", 0)
        last = self.event("custom", 1)
        resilient = ResilientStore.open(":memory:", store_factory=lambda path: self.store)
        try:
            page = resilient.records(limit=1)
            with self.store._db:
                self.store._db.execute("DELETE FROM events WHERE id=?", (last["id"],))
            before = resilient.status()
            with self.assertRaises(RecordCursorExpired):
                resilient.records(limit=1, cursor=page["next_cursor"])
            self.assertTrue(resilient.status()["available"])
            self.assertEqual(resilient.status()["last_error"], before["last_error"])
            with self.assertRaises(ValueError):
                resilient.records(kind="unknown")
            self.assertTrue(resilient.status()["available"])
        finally:
            resilient.close(timeout=1)

    def test_storage_failure_recovers_without_turning_validation_errors_into_disk_errors(self):
        self.event("custom", 0)
        resilient = ResilientStore.open(":memory:", store_factory=lambda path: self.store)
        try:
            with patch.object(self.store, "records", side_effect=sqlite3.DatabaseError("disk I/O error")):
                with self.assertRaises(HistoryUnavailable):
                    resilient.records()
            self.assertFalse(resilient.status()["available"])
            self.assertEqual(resilient.records()["total"], 1)
            self.assertTrue(resilient.status()["available"])
        finally:
            resilient.close(timeout=1)

    def test_export_uses_one_snapshot_and_includes_every_matching_group(self):
        for second in range(601):
            self.event("custom", second)
        iterator = self.store.iter_records(kind="other")
        first = next(iterator)
        self.event("custom", 602, "New after export began")
        records = [first, *iterator]
        self.assertEqual(len(records), 601)
        self.assertNotIn("New after export began", [record["description"] for record in records])


if __name__ == "__main__":
    unittest.main()
