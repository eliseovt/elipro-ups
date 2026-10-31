"""Unified retained register: group evidence before filtering or pagination."""
import base64
import json
import sqlite3
import unittest
from datetime import timedelta
from unittest.mock import patch

from ups_monitor.core import MISSING_TELEMETRY_REASON, RecordCursorExpired, Store, iso_utc, utc_now
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

    def test_recent_summary_groups_beyond_raw_window_and_orders_by_recovery(self):
        opening = self.event("connection_lost", 0)
        for second in range(1, 131):
            self.event("policy_countdown", second)
        self.event("battery_low", 150)
        recovery = self.event("connection_restored", 180)
        self.event("overload", 170)
        self.assertNotIn(opening["id"], [event["id"] for event in self.store.events(100)])

        records = self.store.recent_records()
        incident = records[0]
        self.assertEqual(incident["record_type"], "incident")
        self.assertEqual(incident["opening_event_id"], opening["id"])
        self.assertEqual(incident["recovery_event_id"], recovery["id"])
        self.assertEqual(incident["duration_seconds"], 180)
        self.assertEqual(incident["categories"], ["connection", "protection"])
        self.assertEqual([record.get("kind") for record in records[1:]], ["overload", "battery_low"])
        self.assertTrue(all("events" not in record and "actions" not in record for record in records))
        self.assertEqual(self.store.recent_records(limit=1), [incident])

    def test_recent_summary_reuses_cache_until_events_change_and_returns_isolated_values(self):
        self.event("connection_lost", 0)
        self.event("custom", 1, data={"result": "original"})
        with patch.object(self.store, "_group_records", wraps=self.store._group_records) as grouping:
            first = self.store.recent_records()
            first[0]["data"]["result"] = "changed by caller"
            self.store.add_sample({"timestamp": self.stamp(2), "input_voltage": 220})
            self.store.set_setting("test", True)
            second = self.store.recent_records()
            self.assertEqual(grouping.call_count, 1)
            self.assertEqual(second[0]["data"]["result"], "original")
            self.assertEqual(second[1]["status"], "open")
            self.assertIsNone(second[1]["duration_seconds"], "An unclosed old record has no confirmed duration")
            self.event("connection_restored", 20)
            recovered = self.store.recent_records()[0]
            self.assertEqual(grouping.call_count, 2)
            self.assertEqual(recovered["status"], "recovered")
            self.assertEqual(recovered["duration_seconds"], 20)

    def test_recent_summary_pairs_historical_missing_telemetry_notices_across_raw_window(self):
        self.base = utc_now() - timedelta(days=2)
        first = self.event("policy_unknown", 0, MISSING_TELEMETRY_REASON, {"state": "unknown"})
        for second in range(1, 131):
            self.event("custom", second)
        self.event("policy_unknown", 180, MISSING_TELEMETRY_REASON, {"state": "unknown"})
        first_recovery = self.event("connection_restored", 32683)
        second = self.event("policy_unknown", 40000, MISSING_TELEMETRY_REASON, {"state": "unknown"})
        second_recovery = self.event("connection_restored", 81923)
        self.assertNotIn(first["id"], [event["id"] for event in self.store.events(100)])
        raw_before = self.store.events(1000)
        archive_before = self.store.records(limit=200)["records"]

        records = self.store.recent_records()
        groups = [record for record in records if record["record_type"] == "incident"]
        self.assertEqual([record["type"] for record in groups], ["connection", "connection"])
        self.assertEqual([record["opening_event_id"] for record in groups], [second["id"], first["id"]])
        self.assertEqual([record["recovery_event_id"] for record in groups],
                         [second_recovery["id"], first_recovery["id"]])
        self.assertEqual([record["duration_seconds"] for record in groups], [41923, 32683])
        self.assertTrue(all(record["status"] == "recovered" and record["start_known"] for record in groups))
        self.assertTrue(all(record["categories"] == ["connection", "protection"] for record in groups))
        self.assertFalse(any(record.get("kind") in {"policy_unknown", "connection_restored"} for record in records))
        self.assertEqual(self.store.events(1000), raw_before)
        self.assertEqual(self.store.records(limit=200)["records"], archive_before)

    def test_recent_summary_does_not_infer_missing_telemetry_from_other_unknown_reasons(self):
        cases = [
            ("Esperando una lectura válida del UPS después de actualizar la protección.", {"state": "unknown"}),
            (MISSING_TELEMETRY_REASON + "; causa distinta", {"state": "unknown"}),
            (MISSING_TELEMETRY_REASON, {"state": "countdown"}),
            (MISSING_TELEMETRY_REASON, {}),
        ]
        for index, (message, data) in enumerate(cases):
            self.event("policy_unknown", index * 2, message, data)
            self.event("connection_restored", index * 2 + 1)
        rows = self.store.recent_records()
        self.assertEqual(len(rows), len(cases) * 2)
        self.assertTrue(all(record["record_type"] == "event" for record in rows))
        self.assertTrue(all(record["duration_seconds"] is None for record in rows))

    def test_recent_telemetry_notice_preserves_shared_power_action_and_explicit_opening(self):
        self.event("power_lost", 0)
        opening = self.event("connection_lost", 1)
        notice = self.event("policy_unknown", 2, MISSING_TELEMETRY_REASON, {"state": "unknown"})
        self.event("connection_restored", 20)
        groups = self.store._group_records(list(self.store.iter_events()), utc_now(),
                                          telemetry_notice_ids={notice["id"]})
        groups = {record["type"]: record for record in groups if record["record_type"] == "incident"}
        self.assertEqual(groups["connection"]["opening_event_id"], opening["id"])
        self.assertEqual(groups["connection"]["duration_seconds"], 19)
        for group in groups.values():
            self.assertEqual(group["actions"], [notice])
            self.assertEqual(sum(event["id"] == notice["id"] for event in group["events"]), 1)
        summary = self.store.recent_records()
        self.assertEqual(summary[0]["type"], "connection")
        self.assertEqual(summary[1]["type"], "power")
        self.assertEqual(summary[1]["status"], "open")
        self.assertIsNone(summary[1]["duration_seconds"])

    def test_recent_telemetry_notice_stays_unconfirmed_until_recovery_and_respects_retention(self):
        old = iso_utc(utc_now() - timedelta(days=31))
        self.store.add_event("policy_unknown", MISSING_TELEMETRY_REASON,
                             {"state": "unknown"}, timestamp=old)
        pending = self.store.recent_records()[0]
        self.assertEqual(pending["status"], "open")
        self.assertIsNone(pending["duration_seconds"])
        self.event("connection_restored", 0)
        self.assertEqual(self.store.recent_records()[0]["record_type"], "incident")
        self.store._last_prune = 0
        self.store._prune()
        orphan = self.store.recent_records()[0]
        self.assertEqual(orphan["kind"], "connection_restored")
        self.assertEqual(orphan["record_type"], "event")
        self.assertIsNone(orphan["duration_seconds"])

    def test_legacy_notice_with_intermediate_readings_is_not_a_continuous_gap(self):
        self.event("power_lost", 0)
        notice = self.event("policy_unknown", 10, MISSING_TELEMETRY_REASON, {"state": "unknown"})
        for second in range(20, 100, 10):
            self.store.add_sample({"timestamp": self.stamp(second), "input_voltage": 220})
        recovery = self.event("connection_restored", 120)
        rows = self.store.recent_records()
        self.assertFalse(any(record["type"] == "connection" for record in rows))
        standalone = {record["id"] for record in rows if record["record_type"] == "event"}
        self.assertEqual(standalone, {"event:" + str(notice["id"]), "event:" + str(recovery["id"])})
        self.assertEqual(self.store.records()["records"][-1]["actions"], [notice])

    def test_legacy_gap_allows_the_fresh_sample_that_triggers_recovery(self):
        self.event("policy_unknown", 0, MISSING_TELEMETRY_REASON, {"state": "unknown"})
        self.store.add_sample({"timestamp": self.stamp(119.5), "input_voltage": 220})
        self.event("connection_restored", 120)
        rows = self.store.recent_records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], "connection")
        self.assertEqual(rows[0]["duration_seconds"], 120)

    def test_unclosed_legacy_notice_with_later_measurements_remains_an_event(self):
        self.event("policy_unknown", 0, MISSING_TELEMETRY_REASON, {"state": "unknown"})
        self.store.add_sample({"timestamp": self.stamp(60), "input_voltage": 220})
        rows = self.store.recent_records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["record_type"], "event")
        self.assertIsNone(rows[0]["duration_seconds"])

    def test_recent_cache_rechecks_only_readings_that_can_contradict_a_legacy_gap(self):
        self.event("policy_unknown", 0, MISSING_TELEMETRY_REASON, {"state": "unknown"})
        self.event("connection_restored", 120)
        with patch.object(self.store, "_group_records", wraps=self.store._group_records) as grouping:
            self.assertEqual(self.store.recent_records()[0]["type"], "connection")
            self.store.add_sample({"timestamp": self.stamp(119.5), "input_voltage": 220})
            self.store.add_sample({"timestamp": self.stamp(130), "input_voltage": 220})
            self.assertEqual(self.store.recent_records()[0]["type"], "connection")
            self.assertEqual(grouping.call_count, 1)
            self.store.add_sample({"timestamp": self.stamp(60), "input_voltage": 220})
            self.assertTrue(all(record["record_type"] == "event" for record in self.store.recent_records()))
            self.assertEqual(grouping.call_count, 2)
            self.store.add_sample({"timestamp": self.stamp(70), "input_voltage": 220})
            self.store.recent_records()
            self.assertEqual(grouping.call_count, 2, "An already rejected gap needs no sample-driven rebuild")

    def test_recent_cache_rechecks_an_unclosed_legacy_gap_when_readings_resume(self):
        self.event("policy_unknown", 0, MISSING_TELEMETRY_REASON, {"state": "unknown"})
        with patch.object(self.store, "_group_records", wraps=self.store._group_records) as grouping:
            self.assertEqual(self.store.recent_records()[0]["record_type"], "incident")
            self.store.add_sample({"timestamp": self.stamp(10), "input_voltage": 220})
            self.assertEqual(self.store.recent_records()[0]["record_type"], "event")
            self.assertEqual(grouping.call_count, 2)

    def test_recent_summary_is_invalidated_when_retention_removes_an_opening(self):
        old = iso_utc(utc_now() - timedelta(days=31))
        self.store.add_event("power_lost", "Old opening", timestamp=old)
        self.event("power_restored", 0)
        self.assertEqual(self.store.recent_records()[0]["record_type"], "incident")
        self.store._last_prune = 0
        self.store._prune()
        record = self.store.recent_records()[0]
        self.assertEqual(record["record_type"], "event")
        self.assertEqual(record["kind"], "power_restored")
        self.assertIsNone(record["duration_seconds"])

    def test_overview_separates_power_episodes_after_confirmed_mains_without_inventing_recovery(self):
        older = self.event("power_lost", 0)
        self.event("policy_countdown", 1)
        self.store.add_sample({"timestamp": self.stamp(20), "on_battery": False, "input_voltage": 220})
        newer = self.event("power_lost", 40)
        self.event("policy_countdown", 41)
        cancelled = self.event("policy_cancelled", 42)
        raw_before = self.store.events()

        rows = self.store.recent_records()
        self.assertEqual([row["opening_event_id"] for row in rows], [newer["id"], older["id"]])
        self.assertEqual([row["started_at"] for row in rows], [newer["timestamp"], older["timestamp"]])
        self.assertEqual(rows[0]["action_result"], "cancelled")
        self.assertEqual(rows[1]["action_result"], "none")
        for row in rows:
            self.assertEqual(row["status"], "open")
            self.assertIsNone(row["ended_at"])
            self.assertIsNone(row["duration_seconds"])
        self.assertEqual(self.store.events(), raw_before)
        archive = self.store.records()["records"]
        exported = list(self.store.iter_records())
        self.assertEqual([row["opening_event_id"] for row in archive], [newer["id"], older["id"]])
        self.assertEqual([row["opening_event_id"] for row in exported], [newer["id"], older["id"]])
        self.assertIsNone(archive[1]["duration_seconds"])
        self.assertIsNone(exported[1]["duration_seconds"])
        self.assertEqual(archive[0]["actions"][-1], cancelled)
        self.assertNotIn(cancelled, archive[1]["actions"])

        recovery = self.event("power_restored", 50)
        rows = self.store.recent_records()
        self.assertEqual(rows[0]["opening_event_id"], newer["id"])
        self.assertEqual(rows[0]["recovery_event_id"], recovery["id"])
        self.assertEqual(rows[0]["duration_seconds"], 10)
        self.assertEqual(rows[1]["status"], "open")
        self.assertIsNone(rows[1]["ended_at"])
        self.assertIsNone(rows[1]["duration_seconds"])
        groups = self.store._group_records(list(self.store.iter_events()), utc_now(),
                                          power_restart_ids={newer["id"]})
        self.assertEqual(groups[1]["actions"][-1], cancelled)
        self.assertNotIn(cancelled, groups[0]["actions"])

    def test_overview_does_not_split_repeated_power_loss_without_confirmed_mains(self):
        opening = self.event("power_lost", 0)
        cases = [
            {"on_battery": True, "input_voltage": 220},
            {"on_battery": False, "input_voltage": 0},
            {"on_battery": False, "input_voltage": None},
            {"on_battery": None, "input_voltage": 220},
            {"on_battery": 0, "input_voltage": 220},
            {"on_battery": False, "input_voltage": "220"},
            {"on_battery": False, "input_voltage": True},
        ]
        for index, sample in enumerate(cases, start=1):
            self.store.add_sample({"timestamp": self.stamp(index * 10), **sample})
            self.event("power_lost", index * 10 + 1)
        rows = self.store.recent_records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["opening_event_id"], opening["id"])
        self.assertEqual(rows[0]["status"], "open")
        self.assertIsNone(rows[0]["duration_seconds"])

    def test_overview_power_split_cache_only_rebuilds_for_late_mains_inside_a_joined_interval(self):
        old = self.event("power_lost", 0)
        new = self.event("power_lost", 100)
        with patch.object(self.store, "_group_records", wraps=self.store._group_records) as grouping:
            self.assertEqual(self.store.recent_records()[0]["opening_event_id"], old["id"])
            for second, sample in [
                (50, {"on_battery": True, "input_voltage": 0}),
                (60, {"on_battery": False, "input_voltage": 0}),
                (70, {"on_battery": 0, "input_voltage": 220}),
                (80, {"on_battery": False, "input_voltage": "220"}),
                (110, {"on_battery": False, "input_voltage": 220}),
            ]:
                self.store.add_sample({"timestamp": self.stamp(second), **sample})
                self.store.recent_records()
            self.assertEqual(grouping.call_count, 1)
            self.store.add_sample({"timestamp": self.stamp(90), "on_battery": False, "input_voltage": 220})
            self.assertEqual([row["opening_event_id"] for row in self.store.recent_records()],
                             [new["id"], old["id"]])
            self.assertEqual(grouping.call_count, 2)
            self.store.add_sample({"timestamp": self.stamp(95), "on_battery": False, "input_voltage": 221})
            self.store.recent_records()
            self.assertEqual(grouping.call_count, 2, "An already separated episode needs no sample-driven rebuild")

    def test_overview_power_restarts_are_anchored_to_the_latest_separate_episode(self):
        first = self.event("power_lost", 0)
        self.store.add_sample({"timestamp": self.stamp(20), "on_battery": False, "input_voltage": 220})
        second = self.event("power_lost", 30)
        self.store.add_sample({"timestamp": self.stamp(40), "on_battery": True, "input_voltage": 0})
        self.event("power_lost", 50)
        self.store.add_sample({"timestamp": self.stamp(60), "on_battery": False, "input_voltage": 221})
        third = self.event("power_lost", 70)
        recovery = self.event("power_restored", 80)
        fourth = self.event("power_lost", 90)
        rows = self.store.recent_records()
        self.assertEqual([row["opening_event_id"] for row in rows],
                         [fourth["id"], third["id"], second["id"], first["id"]])
        self.assertEqual(rows[1]["recovery_event_id"], recovery["id"])
        self.assertTrue(all(row["status"] == "open" for row in [rows[0], rows[2], rows[3]]))

    def test_record_cursor_freezes_mains_evidence_against_late_samples(self):
        old = self.event("power_lost", 0)
        new = self.event("power_lost", 100)
        self.event("custom", 110)
        page = self.store.records(limit=1)
        self.assertEqual(page["total"], 2)
        cursor = page["next_cursor"]
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        self.assertEqual(payload["v"], 2)
        self.assertEqual(payload["sample_high"], 0)
        self.assertIsNone(payload["sample_anchor"])
        self.store.add_sample({"timestamp": self.stamp(50), "on_battery": False, "input_voltage": 220})

        frozen = self.store.records(limit=1, cursor=cursor)
        self.assertEqual(frozen["total"], 2)
        self.assertIsNone(frozen["next_cursor"])
        self.assertEqual(frozen["records"][0]["opening_event_id"], old["id"])
        self.assertEqual([event["id"] for event in frozen["records"][0]["events"]], [old["id"], new["id"]])
        fresh = self.store.records()["records"]
        self.assertEqual(len(fresh), 3)
        self.assertEqual([row["opening_event_id"] for row in fresh[1:]], [new["id"], old["id"]])

    def test_record_cursor_rejects_legacy_and_invalid_sample_watermarks(self):
        self.event("custom", 0)
        self.event("custom", 1)
        cursor = self.store.records(limit=1)["next_cursor"]
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        legacy = {key: value for key, value in payload.items() if key not in {"sample_high", "sample_anchor"}}
        legacy["v"] = 1
        encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        with self.assertRaises(RecordCursorExpired):
            self.store.records(cursor=encode(legacy))
        for sample_high in (True, -1, 2 ** 63 - 1, "0", None):
            with self.subTest(sample_high=sample_high), self.assertRaises(ValueError):
                self.store.records(cursor=encode({**payload, "sample_high": sample_high}))
        with self.assertRaises(ValueError):
            self.store.records(cursor=encode({**payload, "sample_anchor": "0" * 64}))
        for sample_anchor in (None, True, "", "a" * 63, "G" * 64):
            with self.subTest(sample_anchor=sample_anchor), self.assertRaises(ValueError):
                self.store.records(cursor=encode({**payload, "sample_high": 1, "sample_anchor": sample_anchor}))

    def test_record_cursor_expires_when_sample_anchor_is_pruned_or_reused(self):
        self.event("power_lost", 0)
        self.event("power_lost", 100)
        self.event("custom", 110)
        stamp = self.stamp(50)
        self.store.add_sample({"timestamp": stamp, "on_battery": True, "input_voltage": 0})
        cursor = self.store.records(limit=1)["next_cursor"]
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        self.assertEqual(payload["sample_high"], 1)
        self.assertEqual(len(payload["sample_anchor"]), 64)
        with self.store._db:
            self.store._db.execute("DELETE FROM samples WHERE id=?", (payload["sample_high"],))
        with self.assertRaises(RecordCursorExpired):
            self.store.records(cursor=cursor)
        self.store.add_sample({"timestamp": stamp, "on_battery": False, "input_voltage": 220})
        self.assertEqual(self.store._db.execute("SELECT MAX(id) FROM samples").fetchone()[0], payload["sample_high"])
        with self.assertRaises(RecordCursorExpired):
            self.store.records(cursor=cursor)
        self.assertEqual(self.store.records()["total"], 3)

    def test_record_cursor_with_sample_anchor_accepts_normal_later_measurements(self):
        old = self.event("power_lost", 0)
        self.event("power_lost", 100)
        self.event("custom", 110)
        self.store.add_sample({"timestamp": self.stamp(50), "on_battery": True, "input_voltage": 0})
        cursor = self.store.records(limit=1)["next_cursor"]
        self.store.add_sample({"timestamp": self.stamp(60), "on_battery": False, "input_voltage": 220})
        self.store.add_sample({"timestamp": self.stamp(120), "on_battery": False, "input_voltage": 221})
        page = self.store.records(cursor=cursor)
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["records"][0]["opening_event_id"], old["id"])
        self.assertEqual(self.store.records()["total"], 3)

    def test_record_export_keeps_its_initial_mains_evidence_snapshot(self):
        old = self.event("power_lost", 0)
        new = self.event("power_lost", 100)
        self.event("custom", 110)
        export = self.store.iter_records()
        self.assertEqual(next(export)["record_type"], "event")
        self.store.add_sample({"timestamp": self.stamp(50), "on_battery": False, "input_voltage": 220})
        remainder = list(export)
        self.assertEqual(len(remainder), 1)
        self.assertEqual([event["id"] for event in remainder[0]["events"]], [old["id"], new["id"]])
        self.assertEqual(len(list(self.store.iter_records())), 3)

    def test_recent_summary_is_bounded_and_resilient_reads_validate_without_disk_errors(self):
        for second in range(105):
            self.event("custom", second)
        resilient = ResilientStore.open(":memory:", store_factory=lambda path: self.store)
        try:
            self.assertEqual(len(resilient.recent_records()), 100)
            for limit in (0, True, 101, "2"):
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    resilient.recent_records(limit)
            self.assertTrue(resilient.status()["available"])
            with patch.object(self.store, "recent_records", side_effect=sqlite3.DatabaseError("disk I/O error")):
                with self.assertRaises(HistoryUnavailable):
                    resilient.recent_records()
            self.assertFalse(resilient.status()["available"])
            self.assertEqual(len(resilient.recent_records(limit=2)), 2)
            self.assertTrue(resilient.status()["available"])
        finally:
            resilient.close(timeout=1)

    def test_pending_events_remain_visible_without_inventing_pairs_and_disappear_after_commit(self):
        resilient = ResilientStore.open(":memory:", store_factory=lambda path: self.store)
        try:
            opening = resilient.add_event("connection_lost", "Sin datos", timestamp=self.stamp(0))
            resilient.add_event("connection_restored", "Recuperada", timestamp=self.stamp(30))
            resilient.add_event("policy_changed", "Configuración guardada", timestamp=self.stamp(31))
            pending = resilient.recent_records()
            self.assertEqual(len(pending), 3)
            self.assertTrue(all(record["record_type"] == "event" and record["pending"] for record in pending))
            self.assertTrue(all(record["duration_seconds"] is None for record in pending))
            self.assertTrue(any(record["id"] == "pending:" + str(-opening["id"]) for record in pending))
            self.assertEqual(len(resilient.events()), 3)
            with resilient._io_lock:
                resilient._persist_batch(tuple(resilient._queue))
            committed = resilient.recent_records()
            self.assertEqual(len(committed), 2)
            self.assertTrue(all("pending" not in record for record in committed))
            self.assertEqual(committed[1]["record_type"], "incident")
            self.assertEqual(committed[1]["duration_seconds"], 30)
            self.assertEqual(resilient.pending_records(), [])
            self.assertEqual(len(resilient.events()), 3)
        finally:
            resilient.close(timeout=1)

    def test_pending_snapshot_is_merged_while_the_commit_lock_is_held(self):
        resilient = ResilientStore.open(":memory:", store_factory=lambda path: self.store)
        try:
            resilient.add_event("simulation", "Aviso de prueba", timestamp=self.stamp(0))
            original = resilient.pending_records

            def locked_pending():
                acquired = resilient._io_lock.acquire(blocking=False)
                if acquired:
                    resilient._io_lock.release()
                self.assertFalse(acquired, "The worker must not commit between archive and pending snapshots")
                return original()

            with patch.object(resilient, "pending_records", side_effect=locked_pending):
                records = resilient.recent_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["kind"], "simulation")
        finally:
            resilient.close(timeout=1)


if __name__ == "__main__":
    unittest.main()
