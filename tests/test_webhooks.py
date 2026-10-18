import copy
import json
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ups_monitor.core import Store
from ups_monitor.webhooks import (DEFAULT_WEBHOOK_SETTINGS, EVENT_DEFINITIONS, HTTP_TIMEOUT_SECONDS,
                                  WebhookManager, _send_json, validate_webhook_settings)


URL = "https://n8n.example.test/webhook/secret-token"
ENABLED = {"webhook_enabled": True, "webhook_url": URL,
           "webhook_events": ["power_lost", "battery_low"]}


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("El worker de webhooks no terminó a tiempo")
        time.sleep(0.005)


class WebhookValidationTests(unittest.TestCase):
    def test_defaults_are_off_and_select_only_power_loss_and_low_battery(self):
        self.assertEqual(validate_webhook_settings({}), DEFAULT_WEBHOOK_SETTINGS)
        self.assertFalse(DEFAULT_WEBHOOK_SETTINGS["webhook_enabled"])
        self.assertEqual(DEFAULT_WEBHOOK_SETTINGS["webhook_events"], ["power_lost", "battery_low"])
        result = validate_webhook_settings({})
        result["webhook_events"].clear()
        self.assertEqual(DEFAULT_WEBHOOK_SETTINGS["webhook_events"], ["power_lost", "battery_low"])

    def test_exactly_five_events_and_empty_selection_are_supported(self):
        self.assertEqual(set(EVENT_DEFINITIONS), {"power_lost", "power_restored", "battery_low", "overload", "ups_fault"})
        self.assertEqual(validate_webhook_settings({**ENABLED, "webhook_events": []})["webhook_events"], [])
        self.assertEqual(EVENT_DEFINITIONS["power_lost"], "critical")
        self.assertEqual(EVENT_DEFINITIONS["power_restored"], "info")

    def test_invalid_settings_are_not_coerced(self):
        invalid = [None, [], {"extra": True}, {"webhook_enabled": 1}, {"webhook_enabled": "true"},
                   {"webhook_url": None}, {"webhook_url": 42}, {"webhook_url": "x" * 2049},
                   {"webhook_events": "power_lost"}, {"webhook_events": ("power_lost",)},
                   {"webhook_events": [True]}, {"webhook_events": [[]]},
                   {"webhook_events": ["connection_lost"]}, {"webhook_events": ["webhook_test"]},
                   {"webhook_events": ["power_lost", "power_lost"]}, {"webhook_enabled": True}]
        for settings in invalid:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                validate_webhook_settings(settings)

    def test_url_requires_http_or_https_destination_without_credentials(self):
        for url in ("file:///etc/passwd", "ftp://example.test", "https://", "example.test", "http://?q=x",
                    "http://host:99999", "http://host:0", "http://[broken", "https://u:p@host/",
                    "https://host/path#fragment", " https://host/", "https://ho\nst/", "https://host/a b"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_webhook_settings({"webhook_url": url})
        for url in ("", URL, "http://localhost:5678/webhook/test", "http://[::1]:5678/webhook"):
            self.assertEqual(validate_webhook_settings({"webhook_url": url})["webhook_url"], url)


class WebhookHTTPTests(unittest.TestCase):
    def test_unsigned_json_post_uses_timeout_and_disables_redirects(self):
        transport, connection = Mock(), Mock()
        response = connection.getresponse.return_value
        response.status = 204
        context, secure = Mock(), Mock()
        context.wrap_socket.return_value = secure
        payload = {"event": "power_lost", "message": "Se interrumpió la energía."}
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))]
        with patch("ups_monitor.webhooks.socket.getaddrinfo", return_value=addresses), \
                patch("ups_monitor.webhooks.socket.socket", return_value=transport), \
                patch("ups_monitor.webhooks.ssl.create_default_context", return_value=context), \
                patch("ups_monitor.webhooks.http.client.HTTPConnection", return_value=connection) as factory:
            self.assertEqual(_send_json(URL, payload), 204)
        self.assertGreater(factory.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(factory.call_args.kwargs["timeout"], 5)
        transport.connect.assert_called_once_with(("127.0.0.1", 443))
        context.wrap_socket.assert_called_once_with(transport, server_hostname="n8n.example.test", do_handshake_on_connect=False)
        secure.do_handshake.assert_called_once_with()
        self.assertEqual(connection.request.call_args.args, ("POST", "/webhook/secret-token"))
        self.assertEqual(connection.request.call_args.kwargs["headers"], {"Content-Type": "application/json"})
        self.assertEqual(json.loads(connection.request.call_args.kwargs["body"]), payload)
        response.close.assert_called_once_with()
        response.read.assert_not_called()
        secure.shutdown.assert_called()

    def test_non_success_http_result_is_returned_without_reading_remote_body(self):
        transport, connection = Mock(), Mock()
        connection.getresponse.return_value.status = 302
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80))]
        with patch("ups_monitor.webhooks.socket.getaddrinfo", return_value=addresses), \
                patch("ups_monitor.webhooks.socket.socket", return_value=transport), \
                patch("ups_monitor.webhooks.http.client.HTTPConnection", return_value=connection) as factory:
            self.assertEqual(_send_json("http://destination.test/path", {"event": "webhook_test"}), 302)
        factory.assert_called_once()
        connection.request.assert_called_once()
        connection.getresponse.return_value.read.assert_not_called()
        connection.getresponse.return_value.close.assert_called_once()

    def test_dns_deadline_cannot_send_late_post_or_spawn_unbounded_resolvers(self):
        entered, release = threading.Event(), threading.Event()
        slots = threading.BoundedSemaphore(1)

        def blocked_dns(*args, **kwargs):
            entered.set()
            release.wait(timeout=2)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80))]

        with patch("ups_monitor.webhooks._DNS_SLOTS", slots), \
                patch("ups_monitor.webhooks.socket.getaddrinfo", side_effect=blocked_dns) as resolver, \
                patch("ups_monitor.webhooks.socket.socket") as make_socket, \
                patch("ups_monitor.webhooks.http.client.HTTPConnection") as connection:
            try:
                for _ in range(2):
                    started = time.monotonic()
                    with self.assertRaises(TimeoutError):
                        _send_json("http://destination.test/", {}, timeout=0.05)
                    self.assertLess(time.monotonic() - started, 0.3)
                self.assertTrue(entered.is_set())
                self.assertEqual(resolver.call_count, 1)
                make_socket.assert_not_called()
                connection.assert_not_called()
            finally:
                release.set()
                wait_for(lambda: slots.acquire(blocking=False))
                slots.release()
            connection.assert_not_called()

    def test_total_deadline_interrupts_slow_response_headers_and_closes_socket(self):
        transport, connection = Mock(), Mock()
        closed = threading.Event()
        transport.shutdown.side_effect = lambda how: closed.set()

        def slow_headers():
            self.assertTrue(closed.wait(timeout=0.5))
            raise OSError("Socket closed by deadline")

        connection.getresponse.side_effect = slow_headers
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80))]
        with patch("ups_monitor.webhooks.socket.getaddrinfo", return_value=addresses), \
                patch("ups_monitor.webhooks.socket.socket", return_value=transport), \
                patch("ups_monitor.webhooks.http.client.HTTPConnection", return_value=connection):
            started = time.monotonic()
            with self.assertRaises(OSError):
                _send_json("http://destination.test/", {}, timeout=0.05)
            self.assertLess(time.monotonic() - started, 0.3)
        connection.request.assert_called_once()
        transport.shutdown.assert_called()
        transport.close.assert_called()
        connection.close.assert_called_once()

    def test_dns_and_connect_share_same_budget(self):
        transport, connection = Mock(), Mock()
        connection.getresponse.return_value.status = 200

        def slow_dns(*args, **kwargs):
            time.sleep(0.04)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80))]

        with patch("ups_monitor.webhooks.socket.getaddrinfo", side_effect=slow_dns), \
                patch("ups_monitor.webhooks.socket.socket", return_value=transport), \
                patch("ups_monitor.webhooks.http.client.HTTPConnection", return_value=connection):
            self.assertEqual(_send_json("http://destination.test/", {}, timeout=0.2), 200)
        for call in transport.settimeout.call_args_list:
            self.assertGreater(call.args[0], 0)
            self.assertLess(call.args[0], 0.18)


class WebhookManagerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.managers = []
        self.blockers = []

    def tearDown(self):
        for blocker in self.blockers:
            blocker.set()
        for manager in self.managers:
            manager.stop()
            if manager._thread is not None:
                manager._thread.join(timeout=1)
        self.store.close()

    def manager(self, *, sender=None, enabled=True, **options):
        if enabled:
            self.store.set_setting("webhooks", copy.deepcopy(ENABLED))
        options.setdefault("retry_delay_seconds", 0.01)
        manager = WebhookManager(self.store, sender=sender, **options)
        self.managers.append(manager)
        return manager

    def finished_test(self, manager):
        wait_for(lambda: manager.status()["test"]["state"] in {"success", "error", "cancelled", "simulated"})
        return manager.status()["test"]

    def test_constructor_primes_settings_without_starting_or_sending(self):
        store = Mock()
        store.get_setting.return_value = copy.deepcopy(ENABLED)
        sender = Mock(return_value=200)
        manager = WebhookManager(store, sender=sender)
        self.managers.append(manager)
        store.get_setting.assert_called_once_with("webhooks", DEFAULT_WEBHOOK_SETTINGS)
        self.assertIsNone(manager._thread)
        sender.assert_not_called()
        status = manager.status()
        self.assertEqual(status, {"settings": ENABLED, "dry_run": False, "test": None, "deliveries": []})
        status["settings"]["webhook_events"].clear()
        self.assertEqual(manager.status()["settings"]["webhook_events"], ENABLED["webhook_events"])

    def test_unreadable_or_invalid_saved_settings_fall_back_to_disabled(self):
        for value in ({"webhook_enabled": True}, {"webhook_events": ["unknown"]}):
            self.store.set_setting("webhooks", value)
            with self.assertLogs("elipro.monitor.webhooks", level="ERROR"):
                manager = self.manager(enabled=False)
            self.assertEqual(manager.status()["settings"], DEFAULT_WEBHOOK_SETTINGS)
        with patch.object(self.store, "get_setting", side_effect=RuntimeError(URL)):
            with self.assertLogs("elipro.monitor.webhooks", level="ERROR") as captured:
                self.manager(enabled=False)
        self.assertNotIn(URL, " ".join(captured.output))

    def test_master_switch_empty_url_unknown_events_and_individual_selection(self):
        sender = Mock(return_value=200)
        manager = self.manager(sender=sender, enabled=False)
        self.assertFalse(manager.on_event("power_lost", "Cut"))
        with self.assertRaises(ValueError):
            manager.test()
        manager.update_settings(ENABLED)
        self.assertFalse(manager.on_event("webhook_test", "Not a monitor event"))
        self.assertFalse(manager.on_event("connection_lost", "Disconnected"))
        self.assertFalse(manager.on_event("power_restored", "Restored"))
        self.assertTrue(manager.on_event("power_lost", "Cut"))
        self.assertTrue(manager.on_event("battery_low", "Low"))
        manager.start()
        wait_for(lambda: sender.call_count == 2)
        self.assertEqual([call.args[1]["event"] for call in sender.call_args_list], ["power_lost", "battery_low"])
        manager.update_settings({"webhook_enabled": False})
        self.assertFalse(manager.on_event("battery_low", "Low"))
        self.assertEqual(manager.status()["settings"]["webhook_url"], URL)
        manager.update_settings({"webhook_url": ""})
        self.assertFalse(manager.on_event("power_lost", "Cut"))

    def test_switch_and_selections_persist_without_erasing_url(self):
        manager = self.manager()
        manager.update_settings({"webhook_events": ["ups_fault", "overload"]})
        manager.update_settings({"webhook_enabled": False})
        expected = {"webhook_enabled": False, "webhook_url": URL, "webhook_events": ["ups_fault", "overload"]}
        self.assertEqual(self.store.get_setting("webhooks"), expected)
        reloaded = self.manager(enabled=False)
        self.assertEqual(reloaded.status()["settings"], expected)
        reloaded.update_settings({"webhook_enabled": True})
        self.assertEqual(reloaded.status()["settings"]["webhook_events"], ["ups_fault", "overload"])

    def test_failed_settings_commit_does_not_publish_unsaved_values(self):
        manager = self.manager()
        with patch.object(self.store, "set_setting", side_effect=RuntimeError("disk busy")):
            with self.assertRaises(RuntimeError):
                manager.update_settings({"webhook_enabled": False})
        self.assertEqual(manager.status()["settings"], ENABLED)

    def test_snapshot_payload_has_current_whitelisted_finite_data_and_utc_envelope(self):
        sender = Mock(return_value=202)
        with patch("ups_monitor.webhooks.socket.gethostname", return_value="UPS-PC"):
            manager = self.manager(sender=sender)
        sample = {"timestamp": "old-sample", "on_battery": True, "battery_voltage": 26.1,
                  "load_percent": 35, "input_voltage": 0, "output_voltage": float("nan"),
                  "frequency_hz": float("inf"), "temperature_c": None,
                  "fault": object(), "raw_status": {"token": "hidden"}, "battery_percent": 10 ** 200,
                  "raw": {"secret": "hidden"}, "access_token": "hidden", "watts_is_estimate": True}
        message = "UPS funcionando con batería: suministro de red interrumpido."
        self.assertTrue(manager.on_event("power_lost", message, sample))
        sample["load_percent"] = 100
        manager.start()
        wait_for(lambda: sender.called)
        payload = sender.call_args.args[1]
        self.assertEqual(sender.call_args.args[0], URL)
        self.assertEqual(sender.call_args.kwargs, {"timeout": HTTP_TIMEOUT_SECONDS})
        self.assertEqual(payload["event"], "power_lost")
        self.assertEqual(payload["severity"], "critical")
        self.assertEqual(payload["message"], message)
        self.assertEqual(payload["hostname"], "UPS-PC")
        self.assertTrue(payload["timestamp"].endswith("Z"))
        self.assertEqual(payload["load_percent"], 35)
        self.assertEqual(payload["battery_voltage"], 26.1)
        self.assertIs(payload["on_battery"], True)
        self.assertEqual(payload["input_voltage"], 0)
        for key in ("output_voltage", "frequency_hz", "temperature_c", "fault", "raw_status", "battery_percent", "raw", "access_token"):
            self.assertNotIn(key, payload)
        json.dumps(payload, allow_nan=False)

    def test_manual_test_starts_worker_uses_saved_url_and_reports_success(self):
        sender = Mock(return_value=204)
        manager = self.manager(sender=sender)
        manager.update_settings({"webhook_events": []})
        queued = manager.test({"on_battery": False, "load_percent": 15})
        self.assertEqual(queued["state"], "queued")
        result = self.finished_test(manager)
        self.assertEqual(result["id"], queued["id"])
        self.assertEqual(result["state"], "success")
        self.assertEqual(result["http_status"], 204)
        self.assertIn("correctamente", result["message"])
        self.assertEqual(sender.call_args.args[0], URL)
        self.assertEqual(sender.call_args.args[1]["event"], "webhook_test")
        self.assertEqual(sender.call_args.args[1]["severity"], "info")
        self.assertIs(sender.call_args.args[1]["on_battery"], False)
        self.assertTrue(manager._thread.daemon)
        manager.stop()
        with self.assertRaises(ValueError):
            manager.test()

    def test_dry_run_blocks_both_automatic_and_manual_posts(self):
        sender = Mock(return_value=200)
        manager = self.manager(sender=sender, dry_run=True)
        manager.start()
        for event in EVENT_DEFINITIONS:
            self.assertFalse(manager.on_event(event, "Event"))
        result = manager.test({"battery_voltage": 26.1})
        self.assertEqual(result["state"], "simulated")
        self.assertIn("--dry-run", result["message"])
        self.assertIn("no se envió", result["message"])
        self.assertTrue(manager.status()["dry_run"])
        self.assertEqual(manager.status()["test"]["state"], "simulated")
        sender.assert_not_called()

    def test_non_2xx_and_exceptions_report_error_without_stopping_worker_or_leaking_url(self):
        sender = Mock(side_effect=[500, 500, TimeoutError(URL + "?token=secret"), TimeoutError(URL), 200])
        manager = self.manager(sender=sender)
        with self.assertLogs("elipro.monitor.webhooks", level="ERROR") as captured:
            manager.test()
            result = self.finished_test(manager)
            self.assertEqual(result["state"], "error")
            self.assertEqual(result["http_status"], 500)
            manager.test()
            result = self.finished_test(manager)
            self.assertEqual(result["state"], "error")
            self.assertNotIn("http_status", result)
        self.assertNotIn("secret", " ".join(captured.output))
        self.assertNotIn(URL, json.dumps(manager.status()["test"]))
        manager.test()
        self.assertEqual(self.finished_test(manager)["state"], "success")
        self.assertEqual(sender.call_count, 5)

    def blocking_manager(self, queue_capacity=1):
        entered, release = threading.Event(), threading.Event()
        self.blockers.append(release)

        def blocked_sender(url, payload, *, timeout):
            entered.set()
            release.wait(timeout=3)
            return 200

        sender = Mock(side_effect=blocked_sender)
        manager = self.manager(sender=sender, queue_capacity=queue_capacity)
        manager.start()
        manager.on_event("power_lost", "First event")
        self.assertTrue(entered.wait(timeout=1))
        return manager, sender, release

    def test_slow_http_uses_bounded_queue_and_never_blocks_producer_or_stop(self):
        manager, sender, release = self.blocking_manager()
        started = time.monotonic()
        self.assertTrue(manager.on_event("battery_low", "Pending event"))
        self.assertFalse(manager.on_event("power_lost", "Queue full"))
        self.assertEqual(manager.test()["state"], "error")
        self.assertLess(time.monotonic() - started, 0.25)
        self.assertEqual(len(manager._pending), 1)
        logged_by = []
        with patch("ups_monitor.webhooks.LOGGER.error", side_effect=lambda *args: logged_by.append(threading.current_thread().name)):
            started = time.monotonic()
            manager.stop()
            self.assertLess(time.monotonic() - started, 0.25)
            self.assertFalse(manager.on_event("battery_low", "Stopped"))
            release.set()
            manager._thread.join(timeout=1)
        self.assertEqual(logged_by, ["elipro-webhooks"])
        self.assertEqual(sender.call_count, 1)

    def test_disable_and_reactivate_invalidate_already_pending_deliveries(self):
        manager, sender, release = self.blocking_manager(queue_capacity=3)
        self.assertTrue(manager.on_event("battery_low", "Pending event"))
        job = manager.test()
        self.assertEqual(job["state"], "queued")
        manager.update_settings({"webhook_enabled": False})
        self.assertEqual(manager.status()["test"]["state"], "cancelled")
        manager.update_settings({"webhook_enabled": True})
        release.set()
        wait_for(lambda: all(record["state"] in {"success", "cancelled"} for record in manager.status()["deliveries"]))
        self.assertEqual(sender.call_count, 1)
        self.assertTrue(manager.on_event("battery_low", "New event"))
        wait_for(lambda: sender.call_count == 2)

    def test_disabling_after_worker_selected_delivery_is_rechecked_before_http(self):
        sender = Mock(return_value=204)
        manager = self.manager(sender=sender)
        selected, release = threading.Event(), threading.Event()
        self.blockers.append(release)
        deliver = manager._deliver

        def pause_before_delivery(item):
            selected.set()
            release.wait(timeout=2)
            deliver(item)

        with patch.object(manager, "_deliver", side_effect=pause_before_delivery):
            manager.on_event("power_lost", "Cut")
            manager.start()
            self.assertTrue(selected.wait(timeout=1))
            self.assertEqual(manager.status()["deliveries"][-1]["state"], "sending")
            manager.update_settings({"webhook_enabled": False})
            release.set()
            wait_for(lambda: manager.status()["deliveries"][-1]["state"] == "cancelled")
        sender.assert_not_called()

    def test_url_and_event_changes_also_invalidate_pending_snapshots(self):
        for changes in ({"webhook_url": "https://new.example.test/webhook"}, {"webhook_events": []}):
            with self.subTest(changes=changes):
                manager, sender, release = self.blocking_manager()
                self.assertTrue(manager.on_event("battery_low", "Old selection"))
                manager.update_settings(changes)
                release.set()
                wait_for(lambda: all(record["state"] in {"success", "cancelled"} for record in manager.status()["deliveries"]))
                self.assertEqual(sender.call_count, 1)
                manager.stop()

    def test_failed_attempt_retries_once_with_same_id_and_payload_after_gap(self):
        attempted_at = []

        def send(url, payload, *, timeout):
            attempted_at.append(time.monotonic())
            return 503 if len(attempted_at) == 1 else 204

        sender = Mock(side_effect=send)
        manager = self.manager(sender=sender, retry_delay_seconds=0.08)
        with self.assertLogs("elipro.monitor.webhooks", level="ERROR"):
            job = manager.test({"load_percent": 20})
            wait_for(lambda: manager.status()["test"]["state"] == "retrying")
            retrying = manager.status()["test"]
            self.assertEqual(retrying["id"], job["id"])
            self.assertEqual(retrying["attempts"], 1)
            self.assertEqual(retrying["max_attempts"], 2)
            self.assertEqual(retrying["http_status"], 503)
            self.assertTrue(retrying["next_retry_at"].endswith("Z"))
            self.assertEqual(manager.status()["deliveries"][-1], retrying)
            result = self.finished_test(manager)
        self.assertEqual(result["id"], job["id"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["state"], "success")
        self.assertEqual(result["http_status"], 204)
        self.assertNotIn("next_retry_at", result)
        self.assertEqual(sender.call_count, 2)
        self.assertGreaterEqual(attempted_at[1] - attempted_at[0], 0.075)
        self.assertEqual(sender.call_args_list[0], sender.call_args_list[1])
        self.assertEqual(len(manager.status()["deliveries"]), 1)

    def test_two_failed_attempts_end_in_error_and_never_schedule_a_third(self):
        sender = Mock(return_value=500)
        manager = self.manager(sender=sender)
        with self.assertLogs("elipro.monitor.webhooks", level="ERROR"):
            manager.test()
            result = self.finished_test(manager)
        self.assertEqual(result["state"], "error")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["max_attempts"], 2)
        self.assertIn("2 intentos", result["message"])
        self.assertNotIn("next_retry_at", result)
        time.sleep(0.03)
        self.assertEqual(sender.call_count, 2)

    def test_retry_wait_does_not_block_fresh_events_and_can_be_cancelled(self):
        sender = Mock(side_effect=[503, 204])
        manager = self.manager(sender=sender, retry_delay_seconds=0.3)
        manager.start()
        with self.assertLogs("elipro.monitor.webhooks", level="ERROR"):
            self.assertTrue(manager.on_event("power_lost", "Cut"))
            wait_for(lambda: manager.status()["deliveries"][0]["state"] == "retrying")
            started = time.monotonic()
            self.assertTrue(manager.on_event("battery_low", "Low"))
            wait_for(lambda: len(manager.status()["deliveries"]) == 2
                     and manager.status()["deliveries"][1]["state"] == "success")
            self.assertLess(time.monotonic() - started, 0.2)
            manager.update_settings({"webhook_enabled": False})
        first, second = manager.status()["deliveries"]
        self.assertEqual((first["event"], first["state"], first["attempts"]), ("power_lost", "cancelled", 1))
        self.assertEqual((second["event"], second["state"], second["attempts"]), ("battery_low", "success", 1))
        self.assertNotIn("next_retry_at", first)
        manager.update_settings({"webhook_enabled": True})
        time.sleep(0.32)
        self.assertEqual(sender.call_count, 2)

    def test_production_defaults_are_five_second_budget_and_retry_gap(self):
        self.assertEqual(HTTP_TIMEOUT_SECONDS, 5)
        self.store.set_setting("webhooks", copy.deepcopy(ENABLED))
        manager = WebhookManager(self.store, sender=Mock(return_value=200))
        self.managers.append(manager)
        self.assertEqual(manager._retry_delay, 5)
        for delay in (0, -1, True, "5", float("nan"), float("inf"), 301):
            with self.subTest(delay=delay), self.assertRaises(ValueError):
                WebhookManager(self.store, retry_delay_seconds=delay)

    def test_delivery_history_is_bounded_and_status_returns_independent_copies(self):
        sender = Mock(return_value=200)
        manager = self.manager(sender=sender, dry_run=True)
        for index in range(60):
            manager.on_event("power_lost", "Cut " + str(index))
        records = manager.status()["deliveries"]
        self.assertEqual(len(records), 50)
        self.assertTrue(all(row["state"] == "simulated" and row["attempts"] == 0 and row["max_attempts"] == 2 for row in records))
        self.assertEqual(len({row["id"] for row in records}), 50)
        self.assertFalse(any("url" in row or "payload" in row for row in records))
        records[0]["message"] = "changed"
        self.assertNotEqual(manager.status()["deliveries"][0]["message"], "changed")
        self.assertEqual(len(manager._dirty), 50)
        sender.assert_not_called()

    def test_store_reads_before_start_and_dirty_records_are_saved_only_by_worker(self):
        store = Mock(spec=["get_setting", "set_setting", "webhook_deliveries", "add_webhook_delivery"])
        store.get_setting.return_value = copy.deepcopy(ENABLED)
        store.webhook_deliveries.return_value = []
        calls = []
        store.add_webhook_delivery.side_effect = lambda record: calls.append((threading.current_thread().name, copy.deepcopy(record)))
        manager = WebhookManager(store, sender=Mock(return_value=204))
        self.managers.append(manager)
        store.webhook_deliveries.assert_called_once_with(limit=50)
        manager.on_event("power_lost", "Cut")
        store.add_webhook_delivery.assert_not_called()
        manager.start()
        wait_for(lambda: any(record["state"] == "success" for _, record in calls))
        self.assertTrue(all(name == "elipro-webhooks" for name, _ in calls))
        self.assertTrue(all("url" not in record and "payload" not in record for _, record in calls))
        self.assertEqual(len({record["id"] for _, record in calls}), 1)

    def test_saved_pending_records_are_cancelled_on_restart_without_replay(self):
        pending = {"id": "old", "event": "power_lost", "state": "retrying", "timestamp": "2026-10-18T10:00:00Z",
                   "attempts": 1, "max_attempts": 2, "message": "Retry later", "next_retry_at": "2026-10-18T10:00:05Z"}
        completed = {"id": "new", "event": "webhook_test", "state": "success", "timestamp": "2026-10-18T10:01:00Z",
                     "attempts": 1, "max_attempts": 2, "message": "Delivered", "http_status": 204}
        store = Mock(spec=["get_setting", "set_setting", "webhook_deliveries", "add_webhook_delivery"])
        store.get_setting.return_value = copy.deepcopy(ENABLED)
        store.webhook_deliveries.return_value = [completed, pending]
        sender = Mock(return_value=200)
        manager = WebhookManager(store, sender=sender)
        self.managers.append(manager)
        records = manager.status()["deliveries"]
        self.assertEqual([row["id"] for row in records], ["old", "new"])
        self.assertEqual(records[0]["state"], "cancelled")
        self.assertNotIn("next_retry_at", records[0])
        self.assertEqual(manager.status()["test"]["id"], "new")
        manager.start()
        wait_for(lambda: store.add_webhook_delivery.called)
        sender.assert_not_called()
        self.assertEqual(store.add_webhook_delivery.call_args.args[0]["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
