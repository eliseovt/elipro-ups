"""Grouped register HTTP/CSV contracts, using isolated history and fake I/O."""
import csv
import io
import json
import sqlite3
from datetime import timedelta
from unittest.mock import patch

from tests.test_reports_api import IsolatedMonitorTests
from ups_monitor.core import iso_utc, utc_now


class RecordsApiTests(IsolatedMonitorTests):
    def capture_csv(self, handler):
        def capture(source, mime, filename):
            source.seek(0)
            handler._send(source.read(), mime, extra={"Content-Disposition": filename})
        handler._send_file = capture

    def test_records_route_returns_grouped_member_search_without_requiring_csrf_for_reads(self):
        self.backend.add_event("power_lost", "Corte")
        action = self.backend.add_event("policy_cancelled", "Acción cancelada por el usuario")
        self.backend.add_event("power_restored", "Regresó la electricidad")
        handler = self.handler("/api/records?kind=protection&q=cancelada", **{"X-UPS-Token": ""})
        handler.do_GET()
        page, code = self.response(handler)
        self.assertEqual(code, 200)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["records"][0]["record_type"], "incident")
        self.assertEqual(page["records"][0]["category"], "power")
        self.assertEqual(page["records"][0]["action_result"], "cancelled")
        self.assertIn(action, page["records"][0]["events"])
        self.assertNotIn(self.monitor.csrf_token, json.dumps(page))

    def test_host_and_origin_gate_both_routes_before_accessing_history(self):
        with patch.object(self.store, "records") as read, patch.object(self.store, "iter_records") as export:
            for path in ("/api/records", "/api/records/export.csv"):
                for headers in ({"Host": "127.0.0.1.evil:8765"}, {"Origin": "https://evil.example"}, {"Origin": "null"}):
                    with self.subTest(path=path, headers=headers):
                        handler = self.handler(path, **headers)
                        handler.do_GET()
                        self.assertEqual(self.response(handler)[1], 403)
            read.assert_not_called()
            export.assert_not_called()

    def test_invalid_options_are_400_without_marking_history_unavailable(self):
        for query in ("kind=power_lost", "kind=power&kind=alerts", "sort=type", "limit=201", "limit=bad",
                      "from=2026-10-18", "q=" + "x" * 201, "cursor=invalid"):
            with self.subTest(query=query):
                handler = self.handler("/api/records?" + query)
                handler.do_GET()
                self.assertEqual(self.response(handler)[1], 400)
                self.assertTrue(self.store.status()["available"])
        for query in ("limit=1", "cursor=anything", "sort=type"):
            with self.subTest(export_query=query):
                handler = self.handler("/api/records/export.csv?" + query)
                handler.do_GET()
                self.assertEqual(self.response(handler)[1], 400)

    def test_records_csv_exports_all_groups_and_literal_member_details_with_formula_text_inert(self):
        for index in range(62):
            self.backend.add_event("power_lost", '=SUM(1,2) "corte"\nBatería')
            self.backend.add_event("policy_cancelled", "Cancelación exclusiva", {"trial": index})
            self.backend.add_event("power_restored", "Regreso de energía")
        self.backend.add_event("custom", "No coincide")
        handler = self.handler("/api/records/export.csv?kind=protection&q=Cancelaci%C3%B3n")
        self.capture_csv(handler)
        handler.do_GET()
        data, mime = handler._send.call_args.args[:2]
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertIn("text/csv", mime)
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 62)
        self.assertEqual(list(rows[0]), ["fecha", "tipo", "descripcion", "estado", "duracion_segundos",
                                        "recuperacion", "proteccion", "detalles"])
        self.assertEqual(rows[0]["descripcion"], '\'=SUM(1,2) "corte"\nBatería')
        self.assertEqual(rows[0]["estado"], "Recuperado")
        self.assertEqual(rows[0]["proteccion"], "Cancelada")
        details = json.loads(rows[0]["detalles"])
        self.assertEqual([event["kind"] for event in details], ["power_lost", "policy_cancelled", "power_restored"])
        self.assertEqual(details[1]["data"]["trial"], 61)

    def test_records_csv_leaves_open_duration_empty_and_preserves_confirmed_duration(self):
        start = utc_now() - timedelta(hours=1)
        self.backend.add_event("power_lost", "Cerrado", timestamp=iso_utc(start))
        self.backend.add_event("power_restored", "Retorno", timestamp=iso_utc(start + timedelta(seconds=75)))
        self.backend.add_event("power_lost", "Abierto", timestamp=iso_utc(start + timedelta(seconds=120)))
        handler = self.handler("/api/records/export.csv")
        self.capture_csv(handler)
        handler.do_GET()
        data = handler._send.call_args.args[0]
        rows = {row["descripcion"]: row for row in csv.DictReader(io.StringIO(data.decode("utf-8-sig")))}
        self.assertEqual(rows["Abierto"]["duracion_segundos"], "")
        self.assertEqual(rows["Abierto"]["recuperacion"], "")
        self.assertEqual(rows["Abierto"]["estado"], "Sin recuperación registrada")
        self.assertEqual(float(rows["Cerrado"]["duracion_segundos"]), 75)
        self.assertTrue(rows["Cerrado"]["recuperacion"])

    def test_records_csv_does_not_publish_unconfirmed_or_invalid_durations(self):
        start = utc_now() - timedelta(minutes=1)
        self.backend.add_event("power_lost", "Corte", timestamp=iso_utc(start))
        self.backend.add_event("power_restored", "Retorno", timestamp=iso_utc(start + timedelta(seconds=15)))
        complete = self.backend.records()["records"][0]
        cases = [
            {"start_known": False},
            {"started_at": None},
            {"ended_at": None},
            {"ended_at": iso_utc(start - timedelta(seconds=1))},
            {"duration_seconds": -1},
            {"duration_seconds": float("nan")},
            {"duration_seconds": True},
            {},
        ]
        records = [{**complete, **changes, "description": f"Caso {index}"}
                   for index, changes in enumerate(cases)]
        handler = self.handler("/api/records/export.csv")
        self.capture_csv(handler)
        with patch.object(self.store, "iter_records", return_value=iter(records)):
            handler.do_GET()
        data = handler._send.call_args.args[0]
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
        self.assertEqual([row["duracion_segundos"] for row in rows[:-1]], [""] * (len(cases) - 1))
        self.assertEqual(float(rows[-1]["duracion_segundos"]), 15)

    def test_records_and_export_failures_are_503_before_download_without_changing_live_monitor(self):
        for path, method in (("/api/records", "records"), ("/api/records/export.csv", "iter_records")):
            with self.subTest(path=path):
                self.store.add_event("custom", "En memoria")
                handler = self.handler(path)
                self.capture_csv(handler)
                with patch.object(self.backend, method, side_effect=sqlite3.DatabaseError("disk I/O error")):
                    handler.do_GET()
                page, code = self.response(handler)
                self.assertEqual(code, 503)
                self.assertEqual(page["history"]["state"], "degraded")
                self.assertEqual(self.monitor.events()[0]["message"], "En memoria")

    def test_expired_record_cursor_is_an_actionable_client_error_and_history_stays_healthy(self):
        self.backend.add_event("custom", "Primero")
        last = self.backend.add_event("custom", "Último")
        cursor = self.backend.records(limit=1)["next_cursor"]
        with self.backend._db:
            self.backend._db.execute("DELETE FROM events WHERE id=?", (last["id"],))
        handler = self.handler("/api/records?limit=1&cursor=" + cursor)
        handler.do_GET()
        page, code = self.response(handler)
        self.assertEqual(code, 400)
        self.assertEqual(page["code"], "record_cursor_expired")
        self.assertIn("Vuelve a consultar el registro", page["error"])
        self.assertTrue(self.store.status()["available"])
