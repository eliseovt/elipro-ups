"""Episode-bound cancellation; hardware, Windows actions and clocks are fakes."""
import io
import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ups_monitor.core import PolicyEngine, Store, iso_utc
from ups_monitor.server import CountdownConflictError, Handler, Monitor


class Clock:
    def __init__(self):
        self.now = 100.0
        self.epoch = datetime.now(timezone.utc)

    def monotonic(self):
        return self.now

    def utc(self):
        return self.epoch + timedelta(seconds=self.now - 100)


class CountdownIdentityTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.actions, self.events = [], []
        self.engine = PolicyEngine({"enabled": True, "countdown_seconds": 30}, self.actions.append,
                                   clock=self.clock.monotonic, utc_clock=self.clock.utc,
                                   event_callback=lambda *event: self.events.append(event))

    def sample(self, **changes):
        return {"timestamp": iso_utc(self.clock.utc()), "on_battery": True,
                "low_battery": False, "battery_percent": 80, **changes}

    def test_identity_is_stable_during_reads_refresh_and_emergency_shortening(self):
        first = self.engine.evaluate(self.sample())
        identifier = first["countdown_id"]
        self.assertRegex(identifier, r"^[0-9a-f]{32}$")
        self.assertTrue(first["cancelable"])
        self.assertEqual(first["countdown_valid_for_ms"], 3000)
        for _ in range(3):
            self.assertEqual(self.engine.status()["countdown_id"], identifier)
        self.clock.now += 2
        self.assertEqual(self.engine.evaluate(self.sample())["countdown_id"], identifier)
        self.clock.now += 1
        shortened = self.engine.evaluate(self.sample(battery_percent=10))
        self.assertEqual(shortened["countdown_id"], identifier)
        self.assertEqual(shortened["countdown_remaining"], 10)
        self.assertEqual(self.events[-1][2]["countdown_id"], identifier)
        self.assertEqual(self.events[-1][2]["countdown_remaining"], 10)
        self.assertEqual(self.events[-1][2]["action"], "shutdown")
        self.assertEqual(self.actions, [])

    def test_reading_status_does_not_refresh_countdown_validity(self):
        identifier = self.engine.evaluate(self.sample())["countdown_id"]
        self.clock.now += 1.25
        self.assertEqual(self.engine.status()["countdown_valid_for_ms"], 1750)
        self.assertEqual(self.engine.status()["countdown_valid_for_ms"], 1750)
        self.clock.now += 1.75
        stale = self.engine.status()
        self.assertEqual(stale["countdown_id"], identifier)
        self.assertEqual(stale["countdown_valid_for_ms"], 0)
        self.assertFalse(stale["cancelable"])
        self.assertEqual(self.actions, [])

    def test_cancel_recovery_loss_policy_changes_and_long_gap_retire_identity(self):
        for reset in ("cancel", "online", "loss", "policy", "gap"):
            with self.subTest(reset=reset):
                engine = PolicyEngine({"enabled": True, "countdown_seconds": 30}, self.actions.append,
                                      clock=self.clock.monotonic, utc_clock=self.clock.utc)
                original = engine.evaluate(self.sample())["countdown_id"]
                if reset == "cancel":
                    engine.cancel()
                    self.assertIsNone(engine.status()["countdown_id"])
                    engine.evaluate(self.sample(on_battery=False))
                elif reset == "online":
                    engine.evaluate(self.sample(on_battery=False))
                    self.assertIsNone(engine.status()["countdown_id"])
                elif reset == "loss":
                    engine.evaluate(None)
                    self.assertIsNone(engine.status()["countdown_id"])
                elif reset == "policy":
                    engine.update_policy({**engine.policy, "countdown_seconds": 40})
                    self.assertIsNone(engine.status()["countdown_id"])
                else:
                    self.clock.now += 16
                current = engine.evaluate(self.sample())["countdown_id"]
                self.assertIsNotNone(current)
                self.assertNotEqual(current, original)
        self.assertEqual(self.actions, [])

    def test_critical_hardware_alarm_still_executes_immediately_and_retires_identity(self):
        identifier = self.engine.evaluate(self.sample())["countdown_id"]
        result = self.engine.evaluate(self.sample(low_battery=True))
        self.assertEqual(result["state"], "action_started")
        self.assertIsNone(result["countdown_id"])
        self.assertEqual(result["countdown_valid_for_ms"], 0)
        self.assertFalse(result["cancelable"])
        critical = [event for event in self.events if event[0] == "policy_countdown"][-1][2]
        self.assertEqual(critical["countdown_id"], identifier)
        self.assertFalse(critical["cancelable"])
        self.assertEqual(self.actions, ["shutdown"])


class CountdownCancellationApiTests(unittest.TestCase):
    path = "/api/protection/countdown/cancel"

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.action = Mock()
        self.current = {"on_battery": True, "low_battery": False, "battery_percent": 80,
                        "fault": False, "load_percent": 10}
        self.adapter = SimpleNamespace(source="test", read=lambda: {"timestamp": iso_utc(), **self.current})
        self.monitor = Monitor(self.store, self.adapter, action_callback=self.action)
        self.addCleanup(self.monitor.stop)
        self.monitor.engine._clock = self.clock.monotonic
        self.monitor.set_policy({"enabled": True, "countdown_seconds": 30})
        self.monitor.tick()
        self.identifier = self.monitor.engine.status()["countdown_id"]

    def handler(self, body, **headers):
        content = json.dumps(body).encode()
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(server_port=8765, monitor=self.monitor)
        handler.path = self.path
        handler.headers = Message()
        values = {"Host": "127.0.0.1:8765", "Content-Type": "application/json",
                  "Content-Length": str(len(content)), "X-UPS-Token": self.monitor.csrf_token,
                  **headers}
        for key, value in values.items():
            handler.headers[key] = value
        handler.rfile = io.BytesIO(content)
        handler._json = Mock()
        return handler

    def test_success_is_bound_to_current_episode_and_preserves_policy_and_history(self):
        before = self.monitor.snapshot()
        self.assertEqual(before["protection"]["countdown_id"], self.identifier)
        self.assertEqual(before["protection"]["valid_for_ms"], 3000)
        handler = self.handler({"countdown_id": self.identifier})
        with patch.object(self.monitor.engine, "evaluate", wraps=self.monitor.engine.evaluate) as evaluate:
            handler.do_POST()
            evaluate.assert_not_called()
        result = handler._json.call_args.args[0]
        self.assertEqual(result["cancelled_countdown_id"], self.identifier)
        self.assertEqual(result["protection"]["state"], "cancelled")
        self.assertIsNone(result["protection"]["countdown_id"])
        self.assertEqual(result["policy"], before["policy"])
        self.assertTrue(any(event["kind"] == "policy_countdown" for event in result["events"]))
        self.assertTrue(any(event["kind"] == "policy_cancelled" for event in result["events"]))
        self.action.assert_not_called()

    def test_expired_episode_returns_conflict_without_evaluating_or_executing(self):
        for advance in (30, 31):
            with self.subTest(advance=advance):
                self.clock.now = 100 + advance
                handler = self.handler({"countdown_id": self.identifier})
                with patch.object(self.monitor.engine, "evaluate") as evaluate:
                    handler.do_POST()
                    evaluate.assert_not_called()
                self.assertEqual(handler._json.call_args.args[1], 409)
                self.assertFalse(self.monitor.engine.status()["suppressed"])
        self.action.assert_not_called()

    def test_live_monitor_cancels_pending_episode_after_snapshot_lease_expires(self):
        self.clock.now += 3.2
        current = self.monitor.engine.status()
        self.assertEqual(current["state"], "countdown")
        self.assertEqual(current["countdown_id"], self.identifier)
        self.assertEqual(current["countdown_valid_for_ms"], 0)
        self.assertFalse(current["cancelable"])
        # Expired snapshot metadata remains unsuitable for advertising a
        # confirmed button; the POST separately validates the current engine.
        self.assertFalse(self.monitor._countdown_actionable(current))
        handler = self.handler({"countdown_id": self.identifier})
        with patch.object(self.monitor.engine, "evaluate", wraps=self.monitor.engine.evaluate) as evaluate, \
                patch.object(self.adapter, "read", wraps=self.adapter.read) as read:
            handler.do_POST()
        evaluate.assert_not_called()
        read.assert_not_called()
        result = handler._json.call_args.args[0]
        self.assertEqual(result["cancelled_countdown_id"], self.identifier)
        self.assertEqual(result["protection"]["state"], "cancelled")
        self.assertTrue(self.monitor.engine.status()["suppressed"])
        self.action.assert_not_called()

    def test_old_client_snapshot_can_request_cancel_after_monitor_received_fresh_reading(self):
        old = self.monitor.snapshot()["protection"]
        self.clock.now += 3.2
        self.monitor.tick()
        current = self.monitor.engine.status()
        self.assertEqual(current["countdown_id"], old["countdown_id"])
        self.assertTrue(current["cancelable"])
        handler = self.handler({"countdown_id": old["countdown_id"]})
        handler.do_POST()
        result = handler._json.call_args.args[0]
        self.assertEqual(result["cancelled_countdown_id"], self.identifier)
        self.assertEqual(result["protection"]["state"], "cancelled")
        self.action.assert_not_called()

    def test_stale_telemetry_still_rejects_live_cancellation_without_reevaluation(self):
        self.clock.now += 3.2
        self.monitor.sample["timestamp"] = iso_utc(datetime.now(timezone.utc) - timedelta(seconds=16))
        handler = self.handler({"countdown_id": self.identifier})
        with patch.object(self.monitor.engine, "evaluate") as evaluate:
            handler.do_POST()
        evaluate.assert_not_called()
        self.assertEqual(handler._json.call_args.args[1], 409)
        self.assertFalse(self.monitor.engine.status()["suppressed"])
        self.action.assert_not_called()

    def test_disconnected_monitor_rejects_live_cancellation_even_with_pending_engine(self):
        self.clock.now += 3.2
        self.monitor.connection = "disconnected"
        handler = self.handler({"countdown_id": self.identifier})
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[1], 409)
        self.assertEqual(self.monitor.engine.status()["countdown_id"], self.identifier)
        self.assertFalse(self.monitor.engine.status()["suppressed"])
        self.action.assert_not_called()

    def test_pending_simulation_rejects_live_cancellation_even_with_pending_engine(self):
        self.clock.now += 3.2
        self.monitor.test_deadline = time.monotonic() + 30
        handler = self.handler({"countdown_id": self.identifier})
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[1], 409)
        self.assertFalse(self.monitor.engine.status()["suppressed"])
        self.action.assert_not_called()

    def test_old_identifier_cannot_cancel_later_countdown(self):
        self.current["on_battery"] = False
        self.monitor.tick()
        self.current["on_battery"] = True
        self.monitor.tick()
        later = self.monitor.engine.status()["countdown_id"]
        self.assertNotEqual(later, self.identifier)
        handler = self.handler({"countdown_id": self.identifier})
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[1], 409)
        self.assertEqual(self.monitor.engine.status()["countdown_id"], later)
        self.assertEqual(self.monitor.engine.status()["state"], "countdown")
        self.action.assert_not_called()

    def test_malformed_identifiers_and_extra_fields_are_rejected(self):
        for body in (None, [], {}, {"countdown_id": None}, {"countdown_id": True},
                     {"countdown_id": "a" * 31}, {"countdown_id": "A" * 32},
                     {"countdown_id": self.identifier, "cancel_all": True}):
            with self.subTest(body=body):
                handler = self.handler(body)
                handler.do_POST()
                self.assertEqual(handler._json.call_args.args[1], 400)
                self.assertEqual(self.monitor.engine.status()["countdown_id"], self.identifier)

    def test_authentication_and_origin_checks_precede_cancellation(self):
        for headers in ({"X-UPS-Token": "incorrect"}, {"Origin": "http://attacker.invalid"},
                        {"Host": "attacker.invalid:8765"}):
            with self.subTest(headers=headers):
                handler = self.handler({"countdown_id": self.identifier}, **headers)
                handler.do_POST()
                self.assertEqual(handler._json.call_args.args[1], 403)
        self.assertEqual(self.monitor.engine.status()["countdown_id"], self.identifier)

    def test_busy_lock_has_bounded_wait_and_does_not_cancel_later(self):
        entered, release = threading.Event(), threading.Event()
        def hold_lock():
            with self.monitor.lock:
                entered.set()
                release.wait(2)
        worker = threading.Thread(target=hold_lock)
        worker.start()
        self.assertTrue(entered.wait(1))
        try:
            started = time.monotonic()
            handler = self.handler({"countdown_id": self.identifier})
            handler.do_POST()
            elapsed = time.monotonic() - started
            self.assertEqual(handler._json.call_args.args[1], 503)
            self.assertLess(elapsed, .75)
        finally:
            release.set()
            worker.join(1)
        self.assertEqual(self.monitor.engine.status()["countdown_id"], self.identifier)
        self.assertFalse(self.monitor.engine.status()["suppressed"])

    def test_episode_is_checked_after_waiting_for_decision_lock(self):
        entered = threading.Event()
        def replace_while_locked():
            with self.monitor.lock:
                entered.set()
                time.sleep(.05)
                self.monitor.engine.evaluate({"timestamp": iso_utc(), "on_battery": False})
                self.monitor.engine.evaluate({"timestamp": iso_utc(), **self.current})
        worker = threading.Thread(target=replace_while_locked)
        worker.start()
        self.assertTrue(entered.wait(1))
        handler = self.handler({"countdown_id": self.identifier})
        handler.do_POST()
        worker.join(1)
        self.assertEqual(handler._json.call_args.args[1], 409)
        current = self.monitor.engine.status()
        self.assertNotEqual(current["countdown_id"], self.identifier)
        self.assertEqual(current["state"], "countdown")
        self.assertFalse(current["suppressed"])
        self.action.assert_not_called()

    def test_deadline_is_checked_after_waiting_for_decision_lock(self):
        self.clock.now = 129.9

        def acquire(*, timeout):
            self.assertEqual(timeout, .25)
            self.clock.now = 130.0  # The deadline elapsed while acquiring the lock.
            return True

        lock = SimpleNamespace(acquire=Mock(side_effect=acquire), release=Mock())
        handler = self.handler({"countdown_id": self.identifier})
        with patch.object(self.monitor, "lock", lock), \
                patch.object(self.monitor.engine, "evaluate") as evaluate:
            handler.do_POST()
        lock.acquire.assert_called_once_with(timeout=.25)
        lock.release.assert_called_once_with()
        evaluate.assert_not_called()
        self.assertEqual(handler._json.call_args.args[1], 409)
        self.assertFalse(self.monitor.engine.status()["suppressed"])
        self.action.assert_not_called()

    def test_history_write_failure_does_not_prevent_guarded_cancel(self):
        with patch.object(self.store, "add_event", side_effect=RuntimeError("disk unavailable")), \
                self.assertLogs("elipro.monitor", level="ERROR"):
            result = self.monitor.cancel_countdown(self.identifier)
        self.assertEqual(result["cancelled_countdown_id"], self.identifier)
        self.assertEqual(result["protection"]["state"], "cancelled")
        self.assertIsNone(result["protection"]["countdown_id"])
        self.action.assert_not_called()

    def test_dry_run_countdown_has_no_actionable_identity_and_cannot_use_real_route(self):
        self.monitor.dry_run = True
        snapshot = self.monitor.snapshot()
        self.assertTrue(snapshot["protection"]["simulation"])
        self.assertIsNone(snapshot["protection"]["countdown_id"])
        self.assertEqual(snapshot["protection"]["valid_for_ms"], 0)
        handler = self.handler({"countdown_id": self.identifier})
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[1], 409)
        self.assertEqual(self.monitor.engine.status()["state"], "countdown")
        self.action.assert_not_called()

    def test_suspension_or_pending_health_failure_disables_native_cancellation(self):
        self.monitor._power_gate.set()
        self.assertIsNone(self.monitor.snapshot()["protection"]["countdown_id"])
        with self.assertRaises(CountdownConflictError):
            self.monitor.cancel_countdown(self.identifier)
        self.monitor._power_gate.clear()
        self.monitor._health_fault.set()
        with self.assertRaises(CountdownConflictError):
            self.monitor.cancel_countdown(self.identifier)
        self.action.assert_not_called()

    def test_immediate_hardware_critical_action_is_not_delayed_by_cancel_route(self):
        self.current["low_battery"] = True
        self.monitor.tick()
        self.action.assert_called_once_with("shutdown")
        handler = self.handler({"countdown_id": self.identifier})
        handler.do_POST()
        self.assertEqual(handler._json.call_args.args[1], 409)
        self.action.assert_called_once_with("shutdown")


if __name__ == "__main__":
    unittest.main()
