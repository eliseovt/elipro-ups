"""Real notification pipeline with mocked probes/HID; no Windows, UDP or sleep."""
import unittest
from unittest.mock import Mock, patch

from ups_monitor.alerts import AlertManager
from ups_monitor.core import DEFAULT_POLICY, Store
from ups_monitor.native_adapter import ForzaProbeReason, ForzaProbeResult, NativeHidAdapter
from ups_monitor.server import Monitor
from ups_monitor.session_agent import Agent
from ups_monitor.session_broker import SessionBroker


PATH = r"\\?\hid#vid_0665&pid_5161#unit#{4d1e55b2-f16f-11cf-88cb-001111000030}"
QS = bytes.fromhex("236701206c206701206c200820500f2012c00020e9203c200920a20d")
ABSENT = ForzaProbeResult(state="absent", reason=ForzaProbeReason.PORT_UNREACHABLE, winerror=10054)
UNKNOWN = ForzaProbeResult(state="unknown", reason=ForzaProbeReason.TIMEOUT)
ACTIVE = ForzaProbeResult(state="active", reason=ForzaProbeReason.RECOGNIZED_REPLY)


class BrokerClient:
    def __init__(self, broker):
        self.broker, self.responses = broker, []

    def connect(self):
        pass

    def poll(self, client_id, after, delivery, **options):
        response = self.broker.poll({"client_id": client_id, "after": after,
                                     "delivery": delivery, "protocol": 2, **options})
        self.responses.append(response)
        return response


class Desktop:
    def __init__(self):
        self.messages, self.receipts = [], []

    def status(self):
        return {"ready": True}

    def set_session_available(self, available):
        self.available = available

    def sync_persistent(self, messages):
        self.persistent = messages

    def notify(self, title, message, **options):
        self.messages.append({"title": title, "message": message, **options})
        self.receipts.append({"delivery_id": options["delivery_id"], "outcome": "accepted"})
        return True

    def drain_receipts(self):
        receipts, self.receipts = self.receipts, []
        return receipts


class StartupProbeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Mock(return_value=100)
        self.probe = self.enterContext(patch("ups_monitor.native_adapter.probe_forza_monitor"))
        self.sleep = self.enterContext(patch("ups_monitor.native_adapter.time.sleep"))
        self.enterContext(patch("ups_monitor.native_adapter.socket.socket", side_effect=AssertionError("No real UDP")))
        self.enterContext(patch("ups_monitor.hid_windows._apis", side_effect=AssertionError("No real Win32/HID")))
        self.enumerate = self.enterContext(patch("ups_monitor.native_adapter.enumerate_ups", return_value=[{
            "path": PATH, "metadata_open": True, "vendor_id": 0x0665, "product_id": 0x5161,
            "input_report_bytes": 9, "output_report_bytes": 9, "feature_report_bytes": 0,
        }]))
        self.transport = self.enterContext(patch("ups_monitor.native_adapter.StatusTransport"))
        self.transport.return_value.query_status.return_value = QS
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.policy = {**DEFAULT_POLICY, "on_battery_seconds": 420}
        self.action = Mock()

    def build_monitor(self):
        self.store.set_setting("policy", self.policy)
        self.broker = SessionBroker(clock=self.clock)
        self.alerts = AlertManager(self.store, self.broker, clock=self.clock)
        self.alerts.start()
        self.addCleanup(self.alerts.stop)
        self.adapter = NativeHidAdapter(clock=self.clock)
        self.monitor = Monitor(self.store, self.adapter, alerts=self.alerts, dry_run=True,
                               action_callback=self.action, health_clock=self.clock)
        self.monitor.engine._clock = self.clock
        self.addCleanup(self.monitor.stop)

    def tick(self, at):
        self.clock.return_value = at
        # Exercise the actual producer, parser, monitor decisions, history and
        # semantic alert synchronization, deterministically without threads.
        self.monitor.tick()
        self.monitor._update_alerts()

    def late_agent(self):
        client, desktop = BrokerClient(self.broker), Desktop()
        agent = Agent(client, desktop, clock=self.clock, session_state=lambda: "active")
        agent.step()
        return agent, client, desktop

    def assert_policy_and_actions_unchanged(self):
        self.assertEqual(self.store.get_setting("policy"), self.policy)
        self.assertEqual(self.monitor.engine.policy, self.policy)
        self.action.assert_not_called()

    def test_transient_unknown_after_first_qs_does_not_create_false_late_desktop_incident(self):
        self.probe.side_effect = [ABSENT, UNKNOWN, ABSENT]
        self.build_monitor()
        self.tick(100)
        initial_events = self.monitor.events()
        self.tick(102)
        self.assertEqual(self.transport.return_value.query_status.call_count, 2)
        self.assertEqual(self.enumerate.call_count, 1)
        self.sleep.assert_called_once_with(0.1)
        self.assertEqual(self.monitor.connection, "connected")
        self.assertIsNone(self.monitor.error)
        self.assertEqual(self.monitor.health.read_failures, 0)
        self.assertEqual(self.monitor.health.read_successes, 2)
        self.assertEqual(self.monitor.events(), initial_events)
        self.assertFalse(any(event["kind"] == "connection_lost" for event in initial_events))
        agent, client, desktop = self.late_agent()
        self.assertEqual(client.responses[-1]["messages"], [])
        self.assertEqual(desktop.messages, [])
        self.assertEqual(self.broker.status()["retained_records"], 0)
        self.assertIsNone(self.monitor.engine.status()["countdown_remaining"])
        self.assert_policy_and_actions_unchanged()

    def test_three_unknowns_fail_closed_and_real_loss_and_recovery_reach_late_agent(self):
        self.probe.side_effect = [ABSENT, UNKNOWN, UNKNOWN, UNKNOWN, ABSENT]
        self.build_monitor()
        self.tick(100)
        self.tick(102)
        self.assertEqual(self.transport.return_value.query_status.call_count, 1)
        self.assertEqual(self.enumerate.call_count, 1)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertTrue(all(call.args == (0.1,) for call in self.sleep.call_args_list))
        self.assertEqual(self.monitor.connection, "disconnected")
        self.assertIsNone(self.monitor.sample)
        self.assertEqual(self.monitor.health.read_failures, 1)
        lost = [event for event in self.monitor.events() if event["kind"] == "connection_lost"]
        self.assertEqual(len(lost), 1)
        self.assertIn("timeout", lost[0]["message"])
        self.assertEqual(self.broker.status()["pending_retained"], 1)
        self.tick(104)
        self.assertEqual(self.monitor.connection, "connected")
        self.assertEqual(self.transport.return_value.query_status.call_count, 2)
        agent, client, desktop = self.late_agent()
        messages = client.responses[-1]["messages"]
        self.assertEqual([item["kind"] for item in messages], ["connection_lost", "connection_restored"])
        self.assertEqual(len(desktop.messages), 2)
        self.assertTrue(all(item["title"] == "Comunicación: aviso registrado" for item in messages))
        self.assertTrue(all("vuelve a entregar datos recientes" in item["message"] for item in messages))
        # A successful desktop receipt still controls acknowledgement; the
        # probe retry change must not discard genuine retained incidents.
        agent.step()
        self.assertEqual(self.broker.status()["pending_retained"], 0)
        self.assertEqual(len(desktop.messages), 2)
        self.assert_policy_and_actions_unchanged()

    def test_active_oem_blocks_immediately_and_retains_a_real_unavailable_notice(self):
        self.probe.return_value = ACTIVE
        self.build_monitor()
        self.tick(100)
        self.probe.assert_called_once()
        self.sleep.assert_not_called()
        self.enumerate.assert_not_called()
        self.transport.assert_not_called()
        self.assertEqual(self.monitor.connection, "disconnected")
        self.assertIsNone(self.monitor.sample)
        self.assertEqual(self.broker.status()["pending_retained"], 1)
        agent, client, desktop = self.late_agent()
        message = client.responses[-1]["messages"][0]
        self.assertEqual(message["kind"], "connection_lost")
        self.assertEqual(message["severity"], "warning")
        self.assertNotIn("vuelve a entregar", message["message"])
        self.assertEqual(len(desktop.messages), 1)
        self.assert_policy_and_actions_unchanged()

    def test_transient_retry_preserves_a_confirmed_countdown_and_real_low_battery_flag(self):
        self.policy = {**self.policy, "enabled": True, "countdown_seconds": 60}
        fields = QS[1:-1].split(b" ")
        fields[9] = b"\xc9"  # Actual QS battery + low-battery flags, decoded by parse_qs.
        self.transport.return_value.query_status.return_value = b"#" + b" ".join(fields) + b"\r"
        self.probe.side_effect = [ABSENT, UNKNOWN, ABSENT]
        self.build_monitor()
        self.tick(100)
        initial = self.monitor.engine.status()
        initial_events = self.monitor.events()
        self.assertEqual(initial["state"], "countdown")
        self.assertEqual(initial["countdown_remaining"], 60)
        self.tick(102)
        current = self.monitor.engine.status()
        self.assertEqual(current["state"], "countdown")
        self.assertEqual(current["countdown_remaining"], 58)
        self.assertIs(self.monitor.sample["low_battery"], True)
        self.assertIsNone(self.monitor.sample["battery_percent"])
        self.assertEqual(self.monitor.events(), initial_events)
        self.assert_policy_and_actions_unchanged()


if __name__ == "__main__":
    unittest.main()
