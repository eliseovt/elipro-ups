"""Hardware-free simulation stays behind independent power-action barriers."""
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ups_monitor.adapters import SimulationAdapter
from ups_monitor.core import DEFAULT_POLICY, Store
from ups_monitor.native_adapter import make_adapter
from ups_monitor.persistence import HistoryUnavailable
from ups_monitor.server import Monitor
from ups_monitor import server, windows_service as service
from tests.test_windows_service import FakeHostAPI


class SimulationTests(unittest.TestCase):
    def test_deterministic_cycle_has_fresh_timestamps_and_never_invents_runtime(self):
        clock = Mock(return_value=100)
        utc = Mock(return_value=datetime.now(timezone.utc))
        adapter = SimulationAdapter(clock=clock, utc_clock=utc)
        for seconds, phase, battery, low in ((0, "online", False, False), (6, "battery", True, False),
                                              (12, "low_battery", True, True), (29, "low_battery", True, True),
                                              (30, "online", False, False), (40, "online", False, False)):
            clock.return_value = 100 + seconds
            utc.return_value += timedelta(seconds=1)
            sample = adapter.read()
            self.assertEqual(sample["raw_status"], "SIMULATION/" + phase)
            self.assertEqual((sample["on_battery"], sample["low_battery"]), (battery, low))
            self.assertEqual(sample["timestamp"], utc.return_value.isoformat())
            self.assertIsNone(sample["runtime_seconds"])
            self.assertIsNone(sample["battery_percent"])
            self.assertTrue(sample["simulated"])

    def test_factory_and_simulated_reads_never_probe_oem_usb_or_network(self):
        with patch("ups_monitor.native_adapter.forza_monitor_running", side_effect=AssertionError("No OEM")), \
             patch("ups_monitor.native_adapter.NativeHidAdapter", side_effect=AssertionError("No native adapter")), \
             patch("ups_monitor.hid_windows._apis", side_effect=AssertionError("No Win32 HID")), \
             patch("socket.socket", side_effect=AssertionError("No network")):
            adapter = make_adapter("simulation", "unused")
            self.assertIsInstance(adapter, SimulationAdapter)
            self.assertTrue(adapter.read()["simulated"])

    def test_full_cycle_runs_real_policy_and_history_but_never_real_action(self):
        store, clock, action = Store(":memory:"), Mock(return_value=100), Mock()
        adapter = SimulationAdapter(clock=clock)
        monitor = Monitor(store, adapter, dry_run=False, action_callback=action)
        monitor.engine._clock = clock
        try:
            self.assertTrue(monitor.dry_run)
            monitor.set_policy({"enabled": True, "countdown_seconds": 10})
            for value, expected in ((100, "online"), (106, "countdown"), (112, "action_started"),
                                    (123, "action_started"), (130, "online")):
                clock.return_value = value
                monitor.tick()
                self.assertEqual(monitor.engine.status()["state"], expected)
                if value == 112:
                    self.assertTrue(monitor.engine.status()["emergency"]["immediate"])
                    self.assertEqual(sum(event["kind"] == "simulation" for event in store.events()), 1)
            kinds = {event["kind"] for event in store.events()}
            self.assertTrue({"power_lost", "battery_low", "simulation", "power_restored"} <= kinds)
            self.assertNotIn("policy_action_started", kinds)
            self.assertEqual(sum(event["kind"] == "simulation" for event in store.events()), 1)
            snapshot = monitor.snapshot()
            self.assertEqual(snapshot["meta"]["source"], "simulation")
            self.assertTrue(snapshot["meta"]["dry_run"])
            self.assertTrue(store.history())
            action.assert_not_called()
        finally:
            monitor.stop()
            store.close()

    def test_adapter_marker_still_blocks_actions_if_dry_run_flag_is_cleared(self):
        store, action = Store(":memory:"), Mock()
        monitor = Monitor(store, SimulationAdapter(), action_callback=action)
        try:
            monitor.dry_run = False
            monitor._act("hibernate")
            action.assert_not_called()
            self.assertEqual(store.events()[0]["kind"], "simulation")
            self.assertTrue(monitor.snapshot()["meta"]["dry_run"])
        finally:
            monitor.stop()
            store.close()

    def test_simulation_usb_info_is_rejected_before_any_resource_or_hardware(self):
        with patch("ups_monitor.server.MonitorInstanceGuard") as ownership, \
             patch("ups_monitor.hid_windows._apis") as usb, patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as result:
                server.main(["--source", "simulation", "--usb-info"])
        self.assertEqual(result.exception.code, 2)
        ownership.assert_not_called()
        usb.assert_not_called()


class SimulationServiceTests(unittest.TestCase):
    def test_registration_simulation_is_explicit_and_rejects_power_capability_before_scm(self):
        vector = service.service_command("data", simulate=True)
        self.assertIn("--simulate", vector)
        self.assertNotIn("--allow-power-actions", vector)
        with patch.object(service, "_apis") as api:
            for options in ({"allow_power_actions": True}, {"self_test": True}):
                with self.assertRaisesRegex(ValueError, "--simulate"):
                    service.manage("install", simulate=True, **options)
            for command in ("start", "status", "stop", "remove"):
                with self.assertRaises(ValueError):
                    service.manage(command, simulate=True)
        api.assert_not_called()

    def test_simulation_service_run_rejects_allow_power_actions_before_host(self):
        with patch.object(service, "ServiceHost") as host, patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as result:
                service.main(["run", "--simulate", "--allow-power-actions"])
        self.assertEqual(result.exception.code, 2)
        host.assert_not_called()

    def test_scm_bootstrap_uses_real_monitor_engine_history_without_any_desktop(self):
        test_root = Path(__file__).resolve().parents[1] / ".test-data"
        test_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_root) as directory:
            data_dir = Path(directory)
            saved = Store(data_dir / "monitor.sqlite3")
            saved.set_setting("policy", {**DEFAULT_POLICY, "enabled": True,
                                         "countdown_seconds": 10})
            saved.close()
            clock = Mock(return_value=100)
            adapter = SimulationAdapter(clock=clock)
            api, power, ownership = FakeHostAPI(), Mock(), Mock()
            results = {}

            class HTTP:
                def __init__(self, address, handler):
                    pass

                def serve_forever(self, **kwargs):
                    monitor = self.monitor
                    results["mode"] = monitor.runtime_mode
                    results["dry_run"] = monitor.dry_run
                    monitor.engine._clock = clock
                    for moment in (100, 106, 112, 123, 130):
                        clock.return_value = moment
                        monitor.tick()
                    # The real persistence worker may own its nonblocking I/O
                    # lock while this deliberately accelerated cycle ends.
                    deadline = time.monotonic() + 2
                    while True:
                        try:
                            monitor.set_policy({"enabled": False})
                            break
                        except HistoryUnavailable:
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(0.01)
                    results["events"] = {item["kind"] for item in monitor.store.events()}
                    results["source"] = monitor.snapshot()["meta"]["source"]

                def shutdown(self):
                    pass

                def server_close(self):
                    pass

            args = SimpleNamespace(data_dir=data_dir, port=8765, self_test=False,
                                   simulate=True, allow_power_actions=False)
            host = service.ServiceHost(args, api=api, target=server.main)
            def factory(source, url):
                self.assertEqual(source, "simulation")
                return adapter
            with patch("ups_monitor.server.make_adapter", side_effect=factory), \
                 patch("ups_monitor.server.MonitorInstanceGuard", return_value=ownership), \
                 patch("ups_monitor.server.Monitor.start", lambda monitor: None), \
                 patch("ups_monitor.server.ThreadingHTTPServer", HTTP), \
                 patch("ups_monitor.server.windows_action", power), \
                 patch("ups_monitor.server.WindowsNotifier") as desktop, \
                 patch("ups_monitor.hid_windows._apis", side_effect=AssertionError("No HID")), \
                 patch("ups_monitor.session_agent.main") as agent, patch("builtins.print"):
                host._main(0, None)
            self.assertEqual(api.states[-1]["exit_code"], 0)
            self.assertEqual(results["mode"], "service")
            self.assertEqual(results["source"], "simulation")
            self.assertTrue(results["dry_run"])
            self.assertIn("simulation", results["events"])
            desktop.assert_not_called()
            agent.assert_not_called()
            power.assert_not_called()
            reopened = Store(data_dir / "monitor.sqlite3")
            try:
                self.assertTrue(reopened.history())
                self.assertFalse(reopened.get_setting("policy")["enabled"])
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
