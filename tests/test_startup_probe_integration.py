"""Real notification pipeline with mocked probes/HID; no Windows, UDP or sleep."""
from datetime import datetime
import json
import unittest
from unittest.mock import Mock, patch

from ups_monitor.alerts import AlertManager
from ups_monitor.core import DEFAULT_POLICY, Store
from ups_monitor.native_adapter import NativeHidAdapter
from ups_monitor.reports import diagnostics
from ups_monitor.server import Monitor
from ups_monitor.session_agent import Agent
from ups_monitor.session_broker import SessionBroker
from ups_monitor.windows_udp import UdpPortReason, UdpPortResult


PATH = r"\\?\hid#vid_0665&pid_5161#unit#{4d1e55b2-f16f-11cf-88cb-001111000030}"
QS = bytes.fromhex("236701206c206701206c200820500f2012c00020e9203c200920a20d")
ABSENT = UdpPortResult(state="absent", reason=UdpPortReason.NO_ENDPOINT)
OCCUPIED = UdpPortResult(state="occupied", reason=UdpPortReason.ENDPOINT_BOUND, pids=(1234,))
UNKNOWN = UdpPortResult(state="unknown", reason=UdpPortReason.API_ERROR, error_code=5, family=23)


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
        self.table = self.enterContext(patch("ups_monitor.native_adapter.udp_port_occupancy"))
        self.sleep = self.enterContext(patch("ups_monitor.native_adapter.time.sleep"))
        self.socket = self.enterContext(patch("ups_monitor.native_adapter.socket.socket"))
        self.udp = self.socket.return_value.__enter__.return_value
        self.udp.recv.side_effect = TimeoutError("Fixture UDP timeout; no real socket")
        self.enterContext(patch("ups_monitor.windows_udp._load_udp_api", side_effect=AssertionError("No real Windows UDP table")))
        self.enterContext(patch("ups_monitor.hid_windows._apis", side_effect=AssertionError("No real Win32/HID")))
        self.enterContext(patch("ups_monitor.health.working_set_bytes", return_value=1_000_000))
        self.enumerate = self.enterContext(patch("ups_monitor.native_adapter.enumerate_ups", return_value=[{
            "path": PATH, "metadata_open": True, "vendor_id": 0x0665, "product_id": 0x5161,
            "input_report_bytes": 9, "output_report_bytes": 9, "feature_report_bytes": 0,
        }]))
        self.transport = self.enterContext(patch("ups_monitor.native_adapter.StatusTransport"))
        self.transport.return_value.query_status.return_value = QS
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.policy = dict(DEFAULT_POLICY)
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

    def assert_safe_ownership_health(self, state, attempt_count):
        health = self.monitor.health_status()
        self.assertEqual(health["state"], "healthy" if state == "absent" else "degraded")
        ownership = health["usb_ownership"]
        self.assertEqual(set(ownership), {"current", "recent"})
        self.assertIsNotNone(ownership["current"])
        self.assertLessEqual(len(ownership["recent"]), 16)
        self.assertEqual(ownership["current"]["result"]["state"], state)
        self.assertEqual(len(ownership["current"]["attempts"]), attempt_count)
        for record in [ownership["current"], *ownership["recent"]]:
            self.assertEqual(set(record), {"checked_at", "duration_ms", "attempts", "result"})
            self.assertTrue(record["checked_at"].endswith("Z"))
            self.assertIsNotNone(datetime.fromisoformat(record["checked_at"].replace("Z", "+00:00")).tzinfo)
            self.assertGreaterEqual(record["duration_ms"], 0)
            self.assertTrue(1 <= len(record["attempts"]) <= 3)
            self.assertEqual(record["result"], record["attempts"][-1])
            for result in [record["result"], *record["attempts"]]:
                self.assertEqual(set(result), {"state", "reason", "winerror", "errno", "pids", "reply_reason", "family"})
                self.assertIsInstance(result["pids"], list)
        encoded = json.dumps(ownership, allow_nan=False)
        self.assertNotIn(PATH, encoded)
        self.assertNotIn("vid_0665", encoded.casefold())
        self.assertNotIn(self.monitor.csrf_token, encoded)
        self.assertNotIn("token", encoded.casefold())
        self.assertNotIn("path", encoded.casefold())
        return ownership

    def test_absent_endpoint_does_not_consult_unresponsive_udp_or_create_late_desktop_incident(self):
        self.table.side_effect = [ABSENT, ABSENT]
        self.build_monitor()
        self.tick(100)
        # A clean first acquisition is already silent; the ownership fix must
        # preserve this without filtering or deleting any broker incident.
        self.assertIsNone(self.alerts.status()["last_notification"])
        self.assertEqual(self.broker.status()["retained_records"], 0)
        initial_events = self.monitor.events()
        self.tick(102)
        self.assertEqual(self.transport.return_value.query_status.call_count, 2)
        self.assertEqual(self.enumerate.call_count, 1)
        self.assertEqual(self.table.call_count, 2)
        self.socket.assert_not_called()
        self.udp.recv.assert_not_called()
        self.sleep.assert_not_called()
        self.assertEqual(self.monitor.connection, "connected")
        self.assertIsNone(self.monitor.error)
        self.assertEqual(self.monitor.health.read_failures, 0)
        self.assertEqual(self.monitor.health.read_successes, 2)
        self.assert_safe_ownership_health("absent", 1)
        self.assertEqual(self.monitor.events(), initial_events)
        self.assertFalse(any(event["kind"] == "connection_lost" for event in initial_events))
        agent, client, desktop = self.late_agent()
        self.assertEqual(client.responses[-1]["messages"], [])
        self.assertEqual(desktop.messages, [])
        self.assertEqual(self.broker.status()["retained_records"], 0)
        self.assertIsNone(self.monitor.engine.status()["countdown_remaining"])
        self.assert_policy_and_actions_unchanged()

    def test_occupied_endpoint_timeout_blocks_usb_and_loss_and_recovery_reach_late_agent(self):
        self.table.side_effect = [ABSENT, OCCUPIED, OCCUPIED, OCCUPIED, ABSENT]
        self.build_monitor()
        self.tick(100)
        self.tick(102)
        self.assertEqual(self.transport.return_value.query_status.call_count, 1)
        self.assertEqual(self.enumerate.call_count, 1)
        self.assertEqual(self.socket.call_count, 3)
        self.assertEqual(self.udp.recv.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertTrue(all(call.args == (0.1,) for call in self.sleep.call_args_list))
        self.assertEqual(self.monitor.connection, "disconnected")
        self.assertIsNone(self.monitor.sample)
        self.assertEqual(self.monitor.health.read_failures, 1)
        ownership = self.assert_safe_ownership_health("unknown", 3)
        self.assertEqual(ownership["current"]["result"]["pids"], [1234])
        self.assertEqual(ownership["current"]["result"]["reply_reason"], "timeout")
        lost = [event for event in self.monitor.events() if event["kind"] == "connection_lost"]
        self.assertEqual(len(lost), 1)
        self.assertIn("USB esté libre", lost[0]["message"])
        self.assertNotIn("está activo", lost[0]["message"])
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

    def test_windows_table_error_blocks_without_udp_and_remains_a_notifiable_failure(self):
        self.table.return_value = UNKNOWN
        self.build_monitor()
        self.tick(100)
        self.assertEqual(self.table.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.socket.assert_not_called()
        self.enumerate.assert_not_called()
        self.transport.assert_not_called()
        self.assertEqual(self.monitor.connection, "disconnected")
        self.assertIsNone(self.monitor.sample)
        self.assertEqual(self.monitor.health.read_failures, 1)
        ownership = self.assert_safe_ownership_health("unknown", 3)
        self.assertEqual(ownership["current"]["result"]["family"], 23)
        self.assertEqual(ownership["current"]["result"]["winerror"], 5)
        self.assertEqual(self.broker.status()["pending_retained"], 1)
        agent, client, desktop = self.late_agent()
        self.assertEqual(client.responses[-1]["messages"][0]["kind"], "connection_lost")
        self.assertEqual(client.responses[-1]["messages"][0]["severity"], "warning")
        self.assertEqual(len(desktop.messages), 1)
        self.assert_policy_and_actions_unchanged()

    def test_active_oem_blocks_immediately_and_retains_a_real_unavailable_notice(self):
        self.table.return_value = OCCUPIED
        self.udp.recv.side_effect = None
        self.udp.recv.return_value = b"monitorOK"
        self.build_monitor()
        self.tick(100)
        self.table.assert_called_once()
        self.socket.assert_called_once()
        self.sleep.assert_not_called()
        self.enumerate.assert_not_called()
        self.transport.assert_not_called()
        self.assertEqual(self.monitor.connection, "disconnected")
        self.assertIsNone(self.monitor.sample)
        self.assert_safe_ownership_health("active", 1)
        self.assertEqual(self.broker.status()["pending_retained"], 1)
        agent, client, desktop = self.late_agent()
        message = client.responses[-1]["messages"][0]
        self.assertEqual(message["kind"], "connection_lost")
        self.assertEqual(message["severity"], "warning")
        self.assertNotIn("vuelve a entregar", message["message"])
        self.assertEqual(len(desktop.messages), 1)
        self.assert_policy_and_actions_unchanged()

    def test_unknown_then_absent_is_exported_as_two_attempts_even_with_unchanged_final_result(self):
        self.table.side_effect = [ABSENT, UNKNOWN, ABSENT]
        self.build_monitor()
        self.tick(100)
        first = self.assert_safe_ownership_health("absent", 1)
        initial_events = self.monitor.events()
        self.tick(102)
        ownership = self.assert_safe_ownership_health("absent", 2)
        self.assertEqual(ownership["current"]["result"], first["current"]["result"])
        self.assertEqual([attempt["state"] for attempt in ownership["current"]["attempts"]], ["unknown", "absent"])
        self.assertEqual(ownership["current"], ownership["recent"][-1])
        self.assertEqual(self.monitor.events(), initial_events)
        self.assertEqual(self.broker.status()["retained_records"], 0)
        self.socket.assert_not_called()
        with patch("ups_monitor.reports.guardian_status", return_value={"available": False}):
            report = diagnostics(self.monitor)
        self.assertEqual(report["health"]["usb_ownership"], ownership)
        encoded = json.dumps(report, allow_nan=False)
        self.assertNotIn(self.monitor.csrf_token, encoded)
        self.assertNotIn("csrf_token", encoded)
        agent, client, desktop = self.late_agent()
        self.assertEqual(client.responses[-1]["messages"], [])
        self.assertEqual(desktop.messages, [])
        self.assert_policy_and_actions_unchanged()

    def test_transient_table_uncertainty_preserves_critical_result_and_real_low_battery_flag(self):
        self.policy = {**self.policy, "enabled": True, "countdown_seconds": 60}
        fields = QS[1:-1].split(b" ")
        fields[9] = b"\xc9"  # Actual QS battery + low-battery flags, decoded by parse_qs.
        self.transport.return_value.query_status.return_value = b"#" + b" ".join(fields) + b"\r"
        self.table.side_effect = [ABSENT, UNKNOWN, ABSENT]
        self.build_monitor()
        self.tick(100)
        initial = self.monitor.engine.status()
        initial_events = self.monitor.events()
        self.assertEqual(initial["state"], "action_started")
        self.assertIsNone(initial["countdown_remaining"])
        self.assertTrue(initial["emergency"]["immediate"])
        self.assertEqual(initial["emergency"]["source"], "ups_critical_alarm")
        self.assertEqual(sum(event["kind"] == "simulation" for event in initial_events), 1)
        self.tick(102)
        current = self.monitor.engine.status()
        self.assertEqual(current["state"], "action_started")
        self.assertIsNone(current["countdown_remaining"])
        self.assertIs(self.monitor.sample["low_battery"], True)
        self.assertIsNone(self.monitor.sample["battery_percent"])
        self.assertEqual(self.monitor.events(), initial_events)
        self.socket.assert_not_called()
        self.sleep.assert_called_once_with(0.1)
        self.assert_policy_and_actions_unchanged()


if __name__ == "__main__":
    unittest.main()
