import unittest
import threading
from datetime import datetime, timedelta, timezone

from ups_monitor.alerts import AlertManager, DEFAULT_NOTIFICATION_SETTINGS, validate_notification_settings
from ups_monitor.core import Store, iso_utc


class Clock:
    def __init__(self):
        self.seconds = 0
        self.epoch = datetime(2026, 10, 17, tzinfo=timezone.utc)

    def monotonic(self):
        return self.seconds

    def utc(self):
        return self.epoch + timedelta(seconds=self.seconds)

    def advance(self, seconds):
        self.seconds += seconds


class Notifier:
    def __init__(self):
        self.messages = []
        self.started = self.stopped = False
        self.accept = True
        self.error = None

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def notify(self, title, message, severity="info", sound=True):
        if self.error:
            raise self.error
        self.messages.append({"title": title, "message": message, "severity": severity, "sound": sound})
        return self.accept

    def status(self):
        return {"state": "running" if self.started and not self.stopped else "stopped"}


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.clock, self.notifier = Clock(), Notifier()
        self.manager = self.new_manager()
        self.manager.start()

    def tearDown(self):
        self.manager.stop()
        self.store.close()

    def new_manager(self):
        return AlertManager(self.store, self.notifier, clock=self.clock.monotonic, utc_clock=self.clock.utc)

    def sample(self, **changes):
        return {"timestamp": iso_utc(self.clock.utc()), "on_battery": False,
                "low_battery": False, "fault": False, "load_percent": 20, **changes}

    def update(self, protection=None, connection="connected", **changes):
        self.manager.update(protection or {"state": "disabled"}, connection, self.sample(**changes))

    def persistent_notifier(self):
        self.notifier.live = {}
        def synchronize(messages):
            self.notifier.live = messages
        def notify(title, message, **options):
            self.notifier.messages.append({"title": title, "message": message, **options})
            return True
        self.notifier.sync_persistent = synchronize
        self.notifier.notify = notify
        self.manager.update_settings({"persistent_critical": True})

    def test_persistent_critical_is_opt_in_and_strictly_boolean(self):
        self.assertFalse(self.manager.status()["settings"]["persistent_critical"])
        for value in (1, "true", None):
            with self.assertRaises(ValueError):
                self.manager.update_settings({"persistent_critical": value})
        self.manager.update_settings({"persistent_critical": True})
        self.manager.update_settings({"sound_enabled": False})
        self.assertTrue(self.new_manager().status()["settings"]["persistent_critical"])

    def test_persistent_conditions_published_before_notice_and_removed_on_recovery(self):
        self.persistent_notifier()
        self.manager.on_event("battery_low", "Batería baja")
        self.assertIn("battery_low", self.notifier.live)
        self.assertTrue(self.notifier.messages[-1]["persistent"])
        self.assertEqual(self.notifier.messages[-1]["key"], "battery_low")
        self.update(low_battery=True)
        self.assertIn("battery_low", self.notifier.live)
        self.update()
        self.assertEqual(self.notifier.live, {})

    def test_persistent_countdown_updates_without_sound_and_clears_on_cancel_or_stale(self):
        self.persistent_notifier()
        protection = {"state": "countdown", "countdown_remaining": 60, "action": "shutdown"}
        self.update(protection, on_battery=True)
        self.assertIn("60 segundos", self.notifier.live["policy_countdown"]["message"])
        self.clock.advance(2)
        protection["countdown_remaining"] = 58
        self.update(protection, on_battery=True)
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("58 segundos", self.notifier.live["policy_countdown"]["message"])
        self.manager.on_event("policy_cancelled", "Cancelado")
        self.assertEqual(self.notifier.live, {})
        self.update(protection, on_battery=True)
        self.manager.update(protection, "disconnected", None)
        self.assertEqual(self.notifier.live, {})

    def test_loss_of_data_or_disabling_notices_removes_all_persistent_conditions(self):
        self.persistent_notifier()
        self.update(low_battery=True, fault=True, load_percent=100)
        self.assertEqual(set(self.notifier.live), {"battery_low", "ups_fault", "overload"})
        self.manager.on_event("connection_lost", "USB desconectado")
        self.assertEqual(self.notifier.live, {})
        self.update(fault=True)
        self.manager.update_settings({"enabled": False})
        self.assertEqual(self.notifier.live, {})

    def test_simulation_and_visual_test_are_never_persistent(self):
        self.persistent_notifier()
        self.manager.on_event("policy_countdown", "Prueba", {"simulation": True})
        self.manager.test()
        self.assertEqual(self.notifier.live, {})
        self.assertTrue(all("persistent" not in message for message in self.notifier.messages))

    def test_startup_connection_and_history_are_silent(self):
        self.store.add_event("power_lost", "Stored incident")
        self.manager.on_event("connection_restored", "Connected")
        for _ in range(3):
            self.manager.status()
            self.update()
        self.assertEqual(self.notifier.messages, [])

    def test_lost_connection_and_recovery_are_reported_once(self):
        self.manager.on_event("connection_lost", "USB unplugged")
        self.manager.on_event("connection_lost", "Another error from the same USB loop")
        self.manager.on_event("connection_restored", "USB available")
        self.manager.on_event("connection_restored", "Still available")
        self.assertEqual(len(self.notifier.messages), 2)
        self.clock.advance(31)
        self.update()
        self.assertEqual(len(self.notifier.messages), 2)

    def test_a_second_power_incident_inside_cooldown_is_not_suppressed(self):
        self.manager.on_event("power_lost", "First power cut")
        self.manager.on_event("power_lost", "Same cut")
        self.clock.advance(10)
        self.manager.on_event("power_restored", "Power returned")
        self.clock.advance(10)
        self.manager.on_event("power_lost", "Second power cut")
        self.manager.on_event("power_lost", "Same second cut")
        self.assertEqual(len(self.notifier.messages), 3)
        self.manager.on_event("power_restored", "Second restoration")
        self.assertEqual(len(self.notifier.messages), 4)

    def test_a_second_usb_incident_inside_cooldown_is_not_suppressed(self):
        self.manager.on_event("connection_lost", "First USB loss")
        self.manager.on_event("connection_lost", "Same loss")
        self.clock.advance(10)
        self.manager.on_event("connection_restored", "USB returned")
        self.clock.advance(10)
        self.manager.on_event("connection_lost", "Second USB loss")
        self.manager.on_event("connection_lost", "Same second loss")
        self.assertEqual(len(self.notifier.messages), 3)
        self.manager.on_event("connection_restored", "Second USB recovery")
        self.assertEqual(len(self.notifier.messages), 4)

    def test_new_critical_conditions_alert_immediately_after_confirmed_recovery(self):
        self.update(low_battery=True, load_percent=100, fault=True)
        self.assertEqual(len(self.notifier.messages), 3)
        self.clock.advance(5)
        self.update(low_battery=False, load_percent=20, fault=False)
        self.clock.advance(5)
        self.manager.on_event("battery_low", "New low battery incident")
        self.manager.on_event("overload", "New overload incident")
        self.manager.on_event("ups_fault", "New UPS fault incident")
        self.update(low_battery=True, load_percent=100, fault=True)
        self.assertEqual(len(self.notifier.messages), 6)

    def test_stopping_does_not_create_disconnect_notification(self):
        self.manager.on_event("connection_lost", "Monitor detenido", {"error_kind": "stopped"})
        self.assertEqual(self.notifier.messages, [])
        self.manager.stop()
        self.manager.on_event("power_lost", "Late event")
        self.update(low_battery=True)
        self.assertEqual(self.notifier.messages, [])

    def test_stop_does_not_hold_manager_lock_while_joining_tray_callback(self):
        completed = threading.Event()

        def tray_callback():
            self.manager.on_event("power_lost", "Late tray callback")
            completed.set()

        def stop_with_callback():
            worker = threading.Thread(target=tray_callback, daemon=True)
            worker.start()
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive(), "A tray callback must not deadlock during shutdown")

        self.notifier.stop = stop_with_callback
        self.manager.stop()
        self.assertTrue(completed.is_set())
        self.assertEqual(self.notifier.messages, [])
        self.assertIsNone(self.manager.status()["last_error"])

    def test_each_event_kind_maps_to_desktop_message(self):
        kinds = ("power_lost", "power_restored", "battery_low", "overload", "ups_fault",
                 "monitor_error", "policy_countdown", "policy_cancelled", "policy_action_failed", "simulation")
        for kind in kinds:
            self.manager.on_event(kind, "New " + kind)
        self.assertEqual(len(self.notifier.messages), len(kinds))
        self.manager.on_event("policy_online", "Routine")
        self.manager.on_event("unsupported", "Routine")
        self.assertEqual(len(self.notifier.messages), len(kinds))

    def test_repeated_critical_event_uses_current_fresh_telemetry(self):
        self.manager.on_event("battery_low", "Battery low")
        self.update(low_battery=True)
        self.assertEqual(len(self.notifier.messages), 1)
        self.clock.advance(29)
        self.update(low_battery=True)
        self.assertEqual(len(self.notifier.messages), 1)
        self.clock.advance(1)
        self.update(low_battery=True)
        self.assertEqual(len(self.notifier.messages), 2)
        self.assertTrue(self.manager.status()["last_notification"]["repeat"])
        self.clock.advance(30)
        self.update(low_battery=False)
        self.clock.advance(30)
        self.update()
        self.assertEqual(len(self.notifier.messages), 2)

    def test_overload_and_fault_repeat_but_never_power_or_usb_loss(self):
        self.manager.on_event("power_lost", "Power lost")
        self.manager.on_event("connection_lost", "Communication lost")
        self.manager.on_event("ups_fault", "Fault")
        self.manager.on_event("overload", "Overload")
        self.clock.advance(30)
        self.update(fault=True, load_percent=100, on_battery=True)
        self.assertEqual(len(self.notifier.messages), 6)
        self.assertEqual([entry["title"] for entry in self.notifier.messages[4:]], ["Sobrecarga del UPS", "Falla del UPS"])

    def test_countdown_does_not_duplicate_event_and_updates_remaining(self):
        self.manager.on_event("policy_countdown", "Tiempo configurado en batería alcanzado", {"state": "countdown"})
        self.update({"state": "countdown", "countdown_remaining": 60, "action": "hibernate"})
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("revisar o cancelar", self.notifier.messages[0]["message"])
        self.clock.advance(30)
        self.update({"state": "countdown", "countdown_remaining": 30, "action": "hibernate"})
        self.assertIn("hibernar Windows en 30 segundos", self.notifier.messages[-1]["message"])
        self.assertEqual(len(self.notifier.messages), 2)
        self.manager.on_event("policy_cancelled", "Cancelled")
        self.clock.advance(30)
        self.update({"state": "cancelled"})
        self.assertEqual(len(self.notifier.messages), 3)

    def test_countdown_event_can_include_action_and_remaining(self):
        self.manager.on_event("policy_countdown", "Tiempo alcanzado", {
            "state": "countdown", "action": "shutdown", "countdown_remaining": 60})
        self.assertIn("apagar Windows en 60 segundos", self.notifier.messages[0]["message"])

    def test_restored_power_and_missing_data_stop_countdown_repeat(self):
        self.manager.on_event("policy_countdown", "Countdown")
        self.manager.on_event("power_restored", "Back online")
        self.clock.advance(31)
        self.update({"state": "online"})
        self.assertEqual(len(self.notifier.messages), 2)
        self.manager.update({"state": "countdown", "countdown_remaining": 20}, "disconnected", None)
        self.assertEqual(len(self.notifier.messages), 2)

    def test_stale_missing_future_or_disconnected_data_cannot_repeat_ups_alerts(self):
        self.manager.on_event("battery_low", "Battery low")
        stale = self.sample(low_battery=True)
        self.clock.advance(30)
        for sample, connection in ((stale, "connected"), (None, "connected"),
                                   (self.sample(low_battery=True, timestamp=iso_utc(self.clock.utc() + timedelta(seconds=10))), "connected"),
                                   (self.sample(low_battery=True), {"state": "disconnected"}),
                                   (self.sample(low_battery=True, connected=False), "connected")):
            self.manager.update({"state": "disabled"}, connection, sample)
        self.assertEqual(len(self.notifier.messages), 1)
        self.update(low_battery=True)
        self.assertEqual(len(self.notifier.messages), 2)

    def test_status_calls_never_emit_notifications(self):
        self.manager.on_event("overload", "Overload")
        self.clock.advance(100)
        for _ in range(100):
            self.manager.status()
        self.assertEqual(len(self.notifier.messages), 1)

    def test_configurable_repeat_interval_can_be_ten_seconds(self):
        self.manager.update_settings({"repeat_seconds": 10})
        self.manager.on_event("ups_fault", "Fault")
        self.clock.advance(10)
        self.update(fault=True)
        self.assertEqual(len(self.notifier.messages), 2)

    def test_settings_persist_preserve_omitted_values_and_do_not_modify_policy(self):
        policy = {"enabled": False}
        self.store.set_setting("policy", policy)
        self.manager.update_settings({"sound_enabled": False, "repeat_seconds": 90})
        self.manager.update_settings({"enabled": False})
        expected = {"enabled": False, "sound_enabled": False, "persistent_critical": False, "repeat_seconds": 90}
        self.assertEqual(self.new_manager().status()["settings"], expected)
        self.assertEqual(self.store.get_setting("policy"), policy)

    def test_disabled_automatic_alerts_and_explicit_visual_test(self):
        self.manager.update_settings({"enabled": False, "sound_enabled": False})
        self.manager.on_event("power_lost", "Power lost")
        self.update(low_battery=True)
        self.assertEqual(self.notifier.messages, [])
        status = self.manager.test()
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertEqual(self.notifier.messages[0]["message"], "Prueba de Elipro. No se apagará ni hibernará tu PC.")
        self.assertFalse(self.notifier.messages[0]["sound"])
        self.assertFalse(status["settings"]["enabled"])
        self.assertEqual(self.store.events(), [])

    def test_explicit_test_is_not_deduplicated_and_sound_uses_settings(self):
        self.manager.test()
        self.manager.test()
        self.assertEqual(len(self.notifier.messages), 2)
        self.assertTrue(self.notifier.messages[0]["sound"])

    def test_rejected_or_failing_notifier_is_reported_without_crashing_monitor(self):
        self.notifier.accept = False
        self.manager.on_event("power_lost", "Power lost")
        self.assertFalse(self.manager.status()["last_notification"]["accepted"])
        self.assertIsNotNone(self.manager.status()["last_error"])
        self.notifier.error = RuntimeError("Backend failure")
        self.manager.on_event("overload", "Overload")
        self.assertIn("Backend failure", self.manager.status()["last_error"])
        self.notifier.error, self.notifier.accept = None, True
        self.manager.test()
        self.assertIsNone(self.manager.status()["last_error"])

    def test_malformed_saved_settings_fall_back_without_modifying_them(self):
        self.store.set_setting("notification_settings", {"enabled": "yes"})
        status = self.new_manager().status()
        self.assertEqual(status["settings"], DEFAULT_NOTIFICATION_SETTINGS)
        self.assertIsNotNone(status["last_error"])
        self.assertEqual(self.store.get_setting("notification_settings"), {"enabled": "yes"})

    def test_strict_settings_validation(self):
        invalid = [None, [], "yes", {"enabled": 1}, {"sound_enabled": "true"},
                   {"repeat_seconds": True}, {"repeat_seconds": 30.0}, {"repeat_seconds": "30"},
                   {"repeat_seconds": 9}, {"repeat_seconds": 301}, {"sound": True}]
        original = self.manager.status()["settings"]
        for settings in invalid:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.manager.update_settings(settings)
            self.assertEqual(self.manager.status()["settings"], original)
        self.assertEqual(validate_notification_settings({}), DEFAULT_NOTIFICATION_SETTINGS)

    def test_low_percent_and_invalid_numbers_are_handled_without_coercion(self):
        self.update(battery_percent=20)
        self.assertEqual(len(self.notifier.messages), 1)
        self.clock.advance(30)
        self.update(battery_percent="20", load_percent=True)
        self.assertEqual(len(self.notifier.messages), 1)
        self.update(battery_percent=float("nan"), load_percent=float("inf"))
        self.assertEqual(len(self.notifier.messages), 1)

    def test_visual_simulation_countdown_never_repeats_protection(self):
        self.manager.on_event("simulation", "Simulation started")
        self.clock.advance(30)
        self.update({"state": "countdown", "countdown_remaining": 10, "simulation": True})
        self.assertEqual(len(self.notifier.messages), 1)

    def test_dry_run_countdown_never_claims_windows_will_shut_down(self):
        self.manager.on_event("policy_countdown", "Time threshold reached", {
            "state": "countdown", "simulation": True, "action": "shutdown", "countdown_remaining": 60})
        notice = self.manager.status()["last_notification"]
        self.assertEqual(notice["kind"], "simulation")
        self.assertIn("No se apagará ni hibernará tu PC", notice["message"])
        self.assertNotIn("solicitará apagar", notice["message"])
        self.clock.advance(30)
        self.update({"state": "countdown", "countdown_remaining": 30, "simulation": True})
        self.assertEqual(len(self.notifier.messages), 1)


if __name__ == "__main__":
    unittest.main()
