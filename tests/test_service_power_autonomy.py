"""Session-zero protection integration. All SCM, USB and OS actions are fakes."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from ups_monitor import server, windows_service as service
from ups_monitor.core import DEFAULT_POLICY, Store, iso_utc
from ups_monitor.session_broker import SessionBroker
from tests.test_windows_service import FakeHostAPI


class ServicePowerAutonomyTests(unittest.TestCase):
    def run_host(self, *, allow_power_actions=False, enabled=False, action="shutdown"):
        """Exercise SCM host -> real server bootstrap -> real Monitor/PolicyEngine."""
        test_root = Path(__file__).resolve().parents[1] / ".test-data"
        test_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_root) as directory:
            data_dir = Path(directory)
            saved = Store(data_dir / "monitor.sqlite3")
            saved.set_setting("policy", {**DEFAULT_POLICY, "enabled": enabled, "action": action,
                                         "countdown_seconds": 10})
            saved.close()
            api, power, ownership = FakeHostAPI(), Mock(), Mock()
            broker = SessionBroker()
            result = {}

            class HTTP:
                def __init__(self, address, handler):
                    pass

                def serve_forever(self, **options):
                    monitor = self.monitor
                    result["runtime_mode"] = monitor.runtime_mode
                    result["dry_run"] = monitor.dry_run
                    # No interactive agent has ever registered or opened a UI.
                    result["desktop_ready"] = broker.status().get("ready", False)
                    clock = Mock(return_value=100)
                    monitor.engine._clock = clock
                    monitor.tick()
                    result["first_state"] = monitor.engine.status()["state"]
                    result["immediate_emergency"] = monitor.engine.status()["emergency"]["immediate"]
                    result["actions_after_first"] = power.call_count
                    clock.return_value = 111
                    monitor.tick()
                    clock.return_value = 112
                    monitor.tick()
                    result["final_state"] = monitor.engine.status()["state"]
                    result["policy_enabled"] = monitor.engine.policy["enabled"]
                    result["events"] = [event["kind"] for event in monitor.store.events()]

                def shutdown(self):
                    pass

                def server_close(self):
                    pass

            args = SimpleNamespace(data_dir=data_dir, port=8765, self_test=False,
                                   allow_power_actions=allow_power_actions)
            adapter = SimpleNamespace(source="test", read=lambda: {
                "timestamp": iso_utc(), "on_battery": True, "low_battery": True})
            host = service.ServiceHost(args, api=api, target=server.main)
            with patch("ups_monitor.server.make_adapter", return_value=adapter), \
                    patch("ups_monitor.server.MonitorInstanceGuard", return_value=ownership), \
                    patch("ups_monitor.server.Monitor.start", lambda monitor: None), \
                    patch("ups_monitor.server.ThreadingHTTPServer", HTTP), \
                    patch("ups_monitor.server.windows_action", power), \
                    patch("ups_monitor.server.WindowsNotifier") as desktop, \
                    patch("ups_monitor.session_broker.SessionBroker", return_value=broker), \
                    patch("ups_monitor.session_agent.main") as agent, patch("builtins.print"):
                host._main(0, None)
            desktop.assert_not_called()
            agent.assert_not_called()
            self.assertEqual(api.states[-1]["state"], service.STOPPED)
            self.assertEqual(api.states[-1]["exit_code"], 0)
            self.assertEqual(result["runtime_mode"], "service")
            self.assertFalse(result["desktop_ready"])
            return result, power

    def test_default_service_keeps_enabled_policy_in_simulation_without_agent(self):
        result, power = self.run_host(enabled=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["first_state"], "action_started")
        self.assertTrue(result["immediate_emergency"])
        self.assertEqual(result["actions_after_first"], 0)
        self.assertIn("simulation", result["events"])
        power.assert_not_called()

    def test_capability_does_not_enable_default_disabled_policy(self):
        result, power = self.run_host(allow_power_actions=True)
        self.assertFalse(result["dry_run"])
        self.assertFalse(result["policy_enabled"])
        self.assertEqual(result["final_state"], "disabled")
        power.assert_not_called()

    def test_critical_battery_runs_one_immediate_service_owned_action_without_any_agent(self):
        for action in ("shutdown", "hibernate"):
            with self.subTest(action=action):
                result, power = self.run_host(allow_power_actions=True, enabled=True, action=action)
                self.assertFalse(result["dry_run"])
                self.assertEqual(result["first_state"], "action_started")
                self.assertTrue(result["immediate_emergency"])
                self.assertEqual(result["actions_after_first"], 1)
                self.assertEqual(result["final_state"], "action_started")
                self.assertIn("policy_action_started", result["events"])
                power.assert_called_once_with(action)


if __name__ == "__main__":
    unittest.main()
