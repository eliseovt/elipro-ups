"""Download and API integration using isolated SQLite and no native I/O."""
import csv
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from email.message import Message
from unittest.mock import Mock, patch
from urllib.parse import urlencode
import zipfile

from ups_monitor.alerts import DEFAULT_NOTIFICATION_SETTINGS
from ups_monitor.core import DEFAULT_POLICY, Store, iso_utc
from ups_monitor.persistence import HistoryUnavailable, ResilientStore
from ups_monitor.reports import backup_archive, csv_cell, diagnostics, save_validation, validation_record
from ups_monitor.server import Handler, Monitor, archive_options, write_events_csv
from ups_monitor.stability import StabilityObservation


TEST_ROOT = Path(__file__).resolve().parents[1] / ".test-data"


class IsolatedMonitorTests(unittest.TestCase):
    def setUp(self):
        TEST_ROOT.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.root = Path(self.directory.name)
        self.backend = Store(self.root / "monitor.sqlite3", busy_timeout_ms=100)
        self.backend.set_setting("policy", {**DEFAULT_POLICY, "on_battery_seconds": 420})
        self.backend.set_setting("notification_settings", {**DEFAULT_NOTIFICATION_SETTINGS, "sound_enabled": False})
        self.store = ResilientStore.open(self.root / "monitor.sqlite3", store_factory=lambda path: self.backend,
                                         retry_seconds=0.01, max_retry_seconds=0.02)
        self.action = Mock()
        self.adapter = SimpleNamespace(source="Lector aislado", bridge_required=False,
                                       read=Mock(return_value={"timestamp": iso_utc(), "on_battery": False}))
        self.monitor = Monitor(self.store, self.adapter, action_callback=self.action, dry_run=True)
        self.monitor.health_status = Mock(return_value={"state": "healthy", "reader_alive": True,
            "supervisor_alive": True, "read_age_seconds": 1, "read_failures": 0,
            "process": {"memory_bytes": 1000, "cpu_percent": 0.1}})
        self.monitor.observation = StabilityObservation(self.monitor, self.root / "stability")

    def tearDown(self):
        self.action.assert_not_called()
        self.adapter.read.assert_not_called()
        self.assertFalse(self.monitor.engine.policy["enabled"])
        self.store.close(timeout=1)
        if self.store._thread is not None:
            self.store._thread.join(timeout=1)
        self.directory.cleanup()

    def handler(self, path, body=None, **headers):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(server_port=8765, monitor=self.monitor)
        handler.path = path
        handler.headers = Message()
        content = json.dumps({} if body is None else body).encode("utf-8")
        values = {"Host": "127.0.0.1:8765", "Content-Type": "application/json",
                  "Content-Length": str(len(content)), "X-UPS-Token": self.monitor.csrf_token}
        values.update(headers)
        for key, value in values.items():
            handler.headers[key] = value
        handler.rfile = io.BytesIO(content)
        handler._send = Mock()
        return handler

    @staticmethod
    def response(handler):
        args, kwargs = handler._send.call_args
        code = args[2] if len(args) > 2 else kwargs.get("code", 200)
        return json.loads(args[0]), code

    def physical_record(self):
        value = {"schema_version": 1,
                 "real_cut": {"status": "passed", "date": "2026-10-17", "seconds_detected": 11.9,
                              "session_preserved_user_confirmed": True, "visual_and_sound_user_confirmed": True},
                 "physical_usb_reconnection": {"status": "pending"},
                 "real_windows_shutdown": {"status": "not_tested_disabled"}}
        (self.root / "validation.json").write_text(json.dumps(value), encoding="utf-8")
        return value


class ReportTests(IsolatedMonitorTests):
    def test_diagnostics_strip_tokens_and_nested_private_keys_without_mutating_source(self):
        snapshot = self.monitor.snapshot()
        snapshot["csrf_token"] = "LIVE-TOKEN-PRIVATE"
        snapshot["health"]["details"] = {"access_token": "NESTED-TOKEN-PRIVATE", "path": "LOCAL-PATH-PRIVATE", "ok": True}
        snapshot["notifications"]["delivery"]["debug"] = [{"device_path": "HID-PATH-PRIVATE", "secret": "SECRET-PRIVATE", "ready": True}]
        snapshot["sample"] = {"battery_voltage": 27.4, "usb_path": "USB-PATH-PRIVATE"}
        snapshot["meta"]["authorization"] = "AUTH-PRIVATE"
        physical = self.physical_record()
        physical["nested"] = {"password": "VALIDATION-PRIVATE", "safe": "keep"}
        (self.root / "validation.json").write_text(json.dumps(physical), encoding="utf-8")
        with patch.object(self.monitor, "snapshot", return_value=snapshot):
            report = diagnostics(self.monitor)
        payload = json.dumps(report)
        for secret in ("LIVE-TOKEN-PRIVATE", "NESTED-TOKEN-PRIVATE", "LOCAL-PATH-PRIVATE", "HID-PATH-PRIVATE",
                       "SECRET-PRIVATE", "USB-PATH-PRIVATE", "AUTH-PRIVATE", "VALIDATION-PRIVATE"):
            self.assertNotIn(secret, payload)
        self.assertEqual(report["current_sample"]["battery_voltage"], 27.4)
        self.assertTrue(report["health"]["details"]["ok"])
        self.assertEqual(snapshot["csrf_token"], "LIVE-TOKEN-PRIVATE")
        self.assertIn("access_token", snapshot["health"]["details"])

    def test_backup_contains_consistent_database_settings_manifest_and_physical_validation(self):
        physical = self.physical_record()
        self.backend.add_event("power_lost", "Corte confirmado")
        self.store.add_event("power_restored", "Pendiente en memoria")
        data = backup_archive(self.monitor)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(set(archive.namelist()), {"history.sqlite3", "configuration.json", "manifest.json", "validation.json", "stability.json", "README.txt"})
            saved = json.loads(archive.read("configuration.json"))
            manifest = json.loads(archive.read("manifest.json"))
            validation = json.loads(archive.read("validation.json"))
            self.assertEqual(saved["policy"]["on_battery_seconds"], 420)
            self.assertFalse(saved["policy"]["enabled"])
            self.assertFalse(saved["notification_settings"]["sound_enabled"])
            self.assertEqual(manifest["pending_in_memory_at_request"], 1)
            self.assertFalse(manifest["restored_automatically"])
            self.assertEqual(validation["record"], physical)
            target = self.root / "downloaded.sqlite3"
            target.write_bytes(archive.read("history.sqlite3"))
        with closing(sqlite3.connect(target)) as database:
            self.assertEqual(database.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(database.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
            actual = {key: json.loads(value) for key, value in database.execute("SELECT key,value FROM settings")}
        self.assertEqual(actual, saved)
        self.assertEqual(manifest["database_snapshot"]["event_count"], 1)

    def test_backup_accepts_relative_data_directory_and_cleans_temporary_folder(self):
        self.store.path = Path(self.store.path).relative_to(Path.cwd())
        data = backup_archive(self.monitor)
        self.assertTrue(data.startswith(b"PK"))
        self.assertEqual(list(self.root.glob("elipro-backup-*")), [])

    def test_corrupt_configuration_is_a_backup_failure_and_gate_is_released(self):
        self.backend._db.execute("UPDATE settings SET value='{' WHERE key='policy'")
        self.backend._db.commit()
        with self.assertRaises(HistoryUnavailable):
            backup_archive(self.monitor)
        self.backend.set_setting("policy", DEFAULT_POLICY)
        self.assertTrue(backup_archive(self.monitor).startswith(b"PK"))

    def test_save_validation_preserves_physical_confirmation_and_reports_missing_or_invalid_files(self):
        self.assertFalse(validation_record(self.monitor)["available"])
        physical = self.physical_record()
        result = {"schema_version": 1, "source": "isolated_simulation", "passed": True}
        self.assertTrue(save_validation(self.monitor, result))
        record = validation_record(self.monitor)["record"]
        self.assertEqual(record["real_cut"], physical["real_cut"])
        self.assertEqual(record["automated_scenarios"], result)
        (self.root / "validation.json").write_text("{broken", encoding="utf-8")
        self.assertFalse(validation_record(self.monitor)["available"])

    def test_csv_formula_text_is_inert_and_normal_values_preserved(self):
        for text in ("=HYPERLINK(\"https://x\")", "+SUM(1)", "-1+1", "@SUM(1)", "  =1", "\tname", "\rname", "\nname"):
            self.assertEqual(csv_cell(text), "'" + text)
        self.assertEqual(csv_cell("Corte de energía"), "Corte de energía")
        self.assertEqual(csv_cell(12), 12)
        self.assertEqual(csv_cell(None), "")

    def test_events_csv_round_trips_quotes_unicode_newlines_and_exports_all_matching_rows(self):
        self.backend.add_event("power_lost", 'UPS, "batería"\nNueva línea', {"value": "á"})
        self.backend.add_event("power_restored", "=1+1")
        target = io.StringIO(newline="")
        write_events_csv(self.store, {}, target)
        rows = list(csv.DictReader(io.StringIO(target.getvalue())))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["message"], 'UPS, "batería"\nNueva línea')
        self.assertEqual(json.loads(rows[0]["data"]), {"value": "á"})
        self.assertEqual(rows[1]["message"], "'=1+1")


class ReportsApiTests(IsolatedMonitorTests):
    def test_archive_routes_return_filtered_events_and_independent_incidents(self):
        self.backend.add_event("power_lost", "Corte")
        self.backend.add_event("connection_lost", "USB desconectado")
        self.backend.add_event("connection_restored", "USB recuperado")
        self.backend.add_event("power_restored", "Volvió la luz")
        handler = self.handler("/api/events/search?" + urlencode({"kind": "power_lost", "q": "Corte", "limit": 1}))
        handler.do_GET()
        payload, code = self.response(handler)
        self.assertEqual(code, 200)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["events"][0]["kind"], "power_lost")
        handler = self.handler("/api/incidents?limit=50")
        handler.do_GET()
        payload, code = self.response(handler)
        self.assertEqual(code, 200)
        self.assertEqual({row["type"] for row in payload["incidents"]}, {"power", "communication"})

    def test_archive_parameters_reject_unknown_duplicate_invalid_range_and_cursor(self):
        paths = ("/api/events/search?limit=201", "/api/events/search?limit=true", "/api/events/search?limit=1&limit=2",
                 "/api/events/search?cursor=bad", "/api/events/search?q=" + "x" * 201,
                 "/api/events/search?extra=1", "/api/events/export.csv?limit=1", "/api/incidents?kind=power_lost",
                 "/api/incidents?from=bad", "/api/events/search?from=2026-10-18T00:00:00Z&to=2026-10-17T00:00:00Z")
        for path in paths:
            with self.subTest(path=path):
                handler = self.handler(path)
                handler.do_GET()
                self.assertEqual(self.response(handler)[1], 400)
        self.assertTrue(self.store.status()["available"])

    def test_archive_aliases_are_exact_and_empty_filters_mean_all_history(self):
        self.assertEqual(archive_options("from=&to=&kind=&q="),
                         {"start": None, "end": None, "kind": None, "query": None, "limit": 50, "cursor": None})
        values = archive_options(urlencode({"from": "2026-10-17T12:00:00Z", "to": "2026-10-17T13:00:00Z", "q": "energía"}), pagination=False)
        self.assertEqual(values["query"], "energía")
        self.assertEqual(values["start"], "2026-10-17T12:00:00Z")

    def test_download_routes_return_download_types_without_csrf_token(self):
        self.physical_record()
        handler = self.handler("/api/diagnostics", **{"X-UPS-Token": ""})
        handler.do_GET()
        data, mime = handler._send.call_args.args[:2]
        self.assertIn("application/json", mime)
        self.assertNotIn(self.monitor.csrf_token, data.decode())
        self.assertIn("attachment", handler._send.call_args.kwargs["extra"]["Content-Disposition"])
        handler = self.handler("/api/backup.zip", **{"X-UPS-Token": ""})
        handler.do_GET()
        data, mime = handler._send.call_args.args[:2]
        self.assertEqual(mime, "application/zip")
        self.assertTrue(data.startswith(b"PK"))
        handler = self.handler("/api/validation")
        handler.do_GET()
        self.assertTrue(self.response(handler)[0]["available"])

    def test_host_and_origin_deny_all_new_read_routes_before_backend_access(self):
        paths = ("/api/events/search", "/api/incidents", "/api/events/export.csv", "/api/diagnostics", "/api/backup.zip", "/api/validation", "/api/stability", "/api/stability/report")
        with patch.object(self.store, "backup") as backup, patch.object(self.store, "events_page") as search:
            for path in paths:
                for headers in ({"Host": "127.0.0.1.evil:8765"}, {"Origin": "https://evil.example"}, {"Origin": "null"}):
                    with self.subTest(path=path, headers=headers):
                        handler = self.handler(path, **headers)
                        handler.do_GET()
                        self.assertEqual(self.response(handler)[1], 403)
            backup.assert_not_called()
            search.assert_not_called()

    def test_validation_and_stability_mutations_require_csrf_and_same_origin(self):
        for path in ("/api/validation/run", "/api/stability/start", "/api/stability/stop"):
            for headers in ({"X-UPS-Token": ""}, {"X-UPS-Token": "wrong"}, {"Origin": "https://evil.example"}):
                with self.subTest(path=path, headers=headers):
                    handler = self.handler(path, **headers)
                    handler.do_POST()
                    self.assertEqual(self.response(handler)[1], 403)
        self.assertEqual(self.monitor.observation.status()["state"], "idle")

    def test_validation_route_runs_only_isolated_scenarios_and_preserves_physical_record(self):
        physical = self.physical_record()
        handler = self.handler("/api/validation/run")
        with patch("ups_monitor.server.windows_action") as windows_action:
            handler.do_POST()
        result, code = self.response(handler)
        self.assertEqual(code, 200)
        self.assertTrue(result["passed"])
        self.assertEqual(result["source"], "isolated_simulation")
        self.assertEqual(result["os_actions_executed"], 0)
        self.assertFalse(result["live_settings_changed"])
        self.assertTrue(result["saved"])
        windows_action.assert_not_called()
        record = validation_record(self.monitor)["record"]
        self.assertEqual(record["real_cut"], physical["real_cut"])
        self.assertTrue(record["automated_scenarios"]["passed"])

    def test_stability_start_validates_body_duration_and_stopped_monitor(self):
        for body in ({"unknown": 1}, [], {"hours": True}, {"hours": "24"}, {"hours": -1}, {"hours": 169}):
            with self.subTest(body=body):
                handler = self.handler("/api/stability/start", body)
                handler.do_POST()
                self.assertEqual(self.response(handler)[1], 400)
        self.monitor.stop_event.set()
        handler = self.handler("/api/stability/start", {"hours": 24})
        handler.do_POST()
        self.assertEqual(self.response(handler)[1], 400)

    def test_stability_routes_use_cached_observation_without_ups_queries(self):
        with patch("ups_monitor.stability.threading.Thread") as thread:
            handler = self.handler("/api/stability/start", {"hours": 24})
            handler.do_POST()
        payload, code = self.response(handler)
        self.assertEqual(code, 200)
        self.assertEqual(payload["state"], "running")
        self.assertEqual(payload["target_seconds"], 86400)
        thread.return_value.start.assert_called_once()
        handler = self.handler("/api/stability")
        handler.do_GET()
        self.assertEqual(self.response(handler)[0]["state"], "running")
        handler = self.handler("/api/stability/report")
        handler.do_GET()
        self.assertIn("application/json", handler._send.call_args.args[1])
        handler = self.handler("/api/stability/stop")
        handler.do_POST()
        self.assertEqual(self.response(handler)[1], 200)

    def test_history_read_failure_is_503_without_changing_protection_or_memory_cache(self):
        self.store.add_event("power_lost", "Corte en memoria")
        with patch.object(self.backend, "events_page", side_effect=sqlite3.OperationalError("disk I/O error")):
            handler = self.handler("/api/events/search")
            handler.do_GET()
        payload, code = self.response(handler)
        self.assertEqual(code, 503)
        self.assertEqual(payload["history"]["state"], "degraded")
        self.assertEqual(self.monitor.events()[0]["message"], "Corte en memoria")

    def test_events_csv_route_outputs_bom_and_quoted_rows_and_errors_before_download(self):
        self.backend.add_event("power_lost", 'Corte, "UPS"\nBatería')
        handler = self.handler("/api/events/export.csv")
        def capture(source, mime, filename):
            source.seek(0)
            handler._send(source.read(), mime, extra={"Content-Disposition": filename})
        handler._send_file = capture
        handler.do_GET()
        data, mime = handler._send.call_args.args[:2]
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertIn("text/csv", mime)
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
        self.assertEqual(rows[0]["message"], 'Corte, "UPS"\nBatería')
        with patch.object(self.backend, "iter_events", side_effect=sqlite3.DatabaseError("malformed")):
            handler = self.handler("/api/events/export.csv")
            handler._send_file = Mock()
            handler.do_GET()
        self.assertEqual(self.response(handler)[1], 503)
        handler._send_file.assert_not_called()

    def test_measurements_csv_route_streams_snapshot_with_formula_text_inert(self):
        self.backend.add_sample({"timestamp": iso_utc(), "on_battery": False,
                                 "battery_voltage": 27.4, "raw_status": "=1+1"})
        handler = self.handler("/api/export.csv")
        def capture(source, mime, filename):
            source.seek(0)
            handler._send(source.read(), mime, extra={"Content-Disposition": filename})
        handler._send_file = capture
        handler.do_GET()
        data, mime = handler._send.call_args.args[:2]
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertIn("text/csv", mime)
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
        self.assertEqual(rows[0]["battery_voltage"], "27.4")
        self.assertEqual(rows[0]["raw_status"], "'=1+1")

    def test_status_exposes_only_valid_estimates_and_history_does_not_backfill_legacy_rows(self):
        for extra, expected in (({}, (None, None)),
                                ({"battery_percent_estimate": 0, "battery_estimate_method": "pmv-v1"}, (0, "pmv-v1")),
                                ({"battery_percent_estimate": True, "battery_estimate_method": "pmv-v1"}, (None, None)),
                                ({"battery_percent_estimate": float("nan"), "battery_estimate_method": "pmv-v1"}, (None, None))):
            with self.subTest(extra=extra):
                self.monitor.sample = {"timestamp": iso_utc(), "on_battery": False,
                                       "battery_voltage": 27.4, **extra}
                self.monitor.connection = "connected"
                handler = self.handler("/api/status")
                handler.do_GET()
                sample = self.response(handler)[0]["sample"]
                self.assertEqual((sample["battery_percent_estimate"], sample["battery_estimate_method"]), expected)
                self.assertEqual(sample["battery_voltage"], 27.4)
        legacy = {"timestamp": iso_utc(), "on_battery": False, "battery_voltage": 27.4}
        self.backend.add_sample(legacy)
        with self.backend._db:
            self.backend._db.execute("UPDATE samples SET payload=?", (json.dumps(legacy),))
        self.backend.add_sample({"timestamp": iso_utc(), "on_battery": True,
                                 "battery_percent_estimate": 45.5, "battery_estimate_method": "pmv-v1"})
        handler = self.handler("/api/history")
        handler.do_GET()
        rows = self.response(handler)[0]["samples"]
        self.assertNotIn("battery_percent_estimate", rows[0])
        self.assertNotIn("battery_estimate_method", rows[0])
        self.assertEqual(rows[1]["battery_percent_estimate"], 45.5)
        self.assertEqual(rows[1]["battery_estimate_method"], "pmv-v1")

    def test_measurements_csv_appends_estimate_columns_without_changing_existing_positions(self):
        legacy = {"timestamp": iso_utc(), "battery_voltage": 27.4, "watts_is_estimate": False}
        self.backend.add_sample(legacy)
        with self.backend._db:
            self.backend._db.execute("UPDATE samples SET payload=?", (json.dumps(legacy),))
        self.backend.add_sample({"timestamp": iso_utc(), "battery_voltage": 26.1,
                                 "battery_percent_estimate": 0, "battery_estimate_method": "pmv-v1"})
        handler = self.handler("/api/export.csv")
        def capture(source, mime, filename):
            source.seek(0)
            handler._send(source.read(), mime, extra={"Content-Disposition": filename})
        handler._send_file = capture
        handler.do_GET()
        data = handler._send.call_args.args[0]
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
        legacy_fields = ["timestamp", "input_voltage", "output_voltage", "frequency_hz", "load_percent",
                         "battery_percent", "battery_voltage", "runtime_seconds", "temperature_c",
                         "on_battery", "low_battery", "fault", "estimated_watts", "raw_status", "watts_is_estimate"]
        self.assertEqual(reader.fieldnames, legacy_fields + ["battery_percent_estimate", "battery_estimate_method"])
        rows = list(reader)
        self.assertEqual(rows[0]["battery_voltage"], "27.4")
        self.assertEqual(rows[0]["battery_percent_estimate"], "")
        self.assertEqual(rows[0]["battery_estimate_method"], "")
        self.assertEqual(rows[1]["battery_percent_estimate"], "0")
        self.assertEqual(rows[1]["battery_estimate_method"], "pmv-v1")


if __name__ == "__main__":
    unittest.main()
