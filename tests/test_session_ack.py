"""Session-zero delivery tests: fake desktops only, never USB or Windows UI."""
import ctypes
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ups_monitor.alerts import AlertManager
from ups_monitor.core import Store
from ups_monitor.session_agent import Agent, _WTSINFOEX, query_session_state
from ups_monitor.session_broker import SessionBroker
from ups_monitor.windows_notifications import WindowsNotifier


CLIENT = "a" * 32


class BrokerV2Tests(unittest.TestCase):
    def setUp(self):
        self.clock = Mock(return_value=10)
        self.broker = SessionBroker(clock=self.clock)
        self.broker.start()

    def poll(self, *, client=CLIENT, state="active", acks=(), epoch=None, after=None):
        return self.broker.poll({"client_id": client, "after": after, "delivery": {"ready": True},
                                 "protocol": 2, "acks": list(acks), "epoch": epoch, "session_state": state})

    def publish(self, kind="power_lost", message="Corte detectado"):
        return self.broker.publish_event(kind, "Aviso", message, "warning")

    def test_no_desktop_retains_electrical_event_and_new_agent_gets_current_summary(self):
        self.assertTrue(self.publish())
        self.assertFalse(self.broker.status()["ready"])
        self.broker.sync_conditions({}, power_state="online", connection_state="connected")
        response = self.poll()
        self.assertEqual(len(response["messages"]), 1)
        self.assertIn("ya se recuperó", response["messages"][0]["message"])
        self.assertEqual(self.broker.status()["retention_storage"], "memory-until-service-stop")

    def test_read_cursor_never_acknowledges_and_ack_requires_offer_current_epoch_active(self):
        initial = self.poll(state="locked")
        self.publish()
        self.clock.return_value = 30  # Longer than legacy TTL and heartbeat timeout.
        self.assertEqual(self.poll(state="locked")["messages"], [])
        first = self.poll()
        identifier = first["messages"][0]["id"]
        self.assertEqual(len(self.poll(after=first["cursor"])["messages"]), 1)
        self.assertEqual(self.poll(state="locked", acks=[identifier], epoch=first["epoch"])["messages"], [])
        self.assertEqual(len(self.poll()["messages"]), 1)
        self.assertEqual(len(self.poll(acks=[identifier], epoch="f" * 32)["messages"]), 1)
        self.assertEqual(self.poll(acks=[identifier], epoch=first["epoch"])["messages"], [])
        self.assertEqual(self.broker.status()["pending_retained"], 0)
        self.assertEqual(self.broker.status()["retained_records"], 1)
        self.assertEqual(initial["protocol"], 2)

    def test_ack_per_session_does_not_steal_locked_desktop_delivery(self):
        first = self.poll()
        self.poll(client="b" * 32, state="locked")
        self.publish()
        message = self.poll()["messages"][0]
        self.poll(acks=[message["id"]], epoch=first["epoch"])
        self.assertEqual(len(self.poll(client="b" * 32)["messages"]), 1)
        self.assertEqual(self.poll(client="c" * 32)["messages"], [])

    def test_stale_countdown_is_summary_and_current_countdown_never_replays_old_seconds(self):
        self.broker.sync_conditions({"policy_countdown": {"title": "Protección", "message": "Apagará en 20 segundos"}})
        self.publish("policy_countdown", "Apagará en 20 segundos")
        current = self.poll()["messages"][0]
        self.assertIn("activa", current["message"])
        self.assertNotIn("20 segundos", current["message"])
        self.broker.sync_conditions({})
        old = self.poll()["messages"][0]
        self.assertIn("anterior", old["title"])
        self.assertIn("no hay", old["message"])
        self.assertNotIn("Apagará", old["message"])

    def test_context_expires_even_when_consumer_never_updates_it_again(self):
        self.broker.sync_conditions({"battery_low": {"title": "Batería baja", "message": "Activa"}}, power_state="online")
        self.publish("battery_low")
        self.clock.return_value = 14
        self.assertIn("registro anterior", self.poll()["messages"][0]["title"])

    def test_repetitions_coalesce_pending_alarm_but_new_incident_stays_separate(self):
        self.publish("battery_low")
        for _ in range(100):
            self.broker.publish_event("battery_low", "Baja", "Continúa baja", repeat=True)
        self.assertEqual(self.broker.status()["retained_records"], 1)
        self.publish("battery_low", "Nueva condición después de recuperarse")
        self.assertEqual(self.broker.status()["retained_records"], 2)

    def test_retention_capacity_expiration_and_stop_are_bounded(self):
        for number in range(100):
            self.publish(message=str(number))
        self.assertEqual(len(self.poll()["messages"]), 32)
        self.assertEqual(self.broker.status()["retained_dropped"], 68)
        self.clock.return_value = 86411
        self.assertEqual(self.poll()["messages"], [])
        self.assertEqual(self.broker.status()["retained_records"], 0)
        self.publish()
        self.broker.stop()
        self.assertEqual(self.broker.status()["retained_records"], 0)

    def test_only_ids_in_returned_page_count_as_offered(self):
        broker = SessionBroker(clock=self.clock, capacity=64)
        broker.start()
        for _ in range(64):
            broker.publish_event("power_lost", "Corte", "Mensaje")
        response = broker.poll({"client_id": CLIENT, "after": None, "delivery": {"ready": True},
                                "protocol": 2, "acks": [], "epoch": None, "session_state": "active"})
        self.assertEqual(len(response["messages"]), 32)
        self.assertEqual(broker._receivers[CLIENT]["offered"], {item["id"] for item in response["messages"]})

    def test_invalid_protocol_ack_and_session_shapes_are_rejected(self):
        base = {"client_id": CLIENT, "after": None, "delivery": {"ready": True},
                "protocol": 2, "acks": [], "epoch": None, "session_state": "active"}
        for change in ({"acks": [True]}, {"acks": list(range(1, 66))}, {"protocol": True},
                       {"epoch": "bad"}, {"session_state": {}}, {"session_state": "unlocked"}):
            with self.assertRaises(ValueError):
                self.broker.poll({**base, **change})


class ReceiptNotifier:
    def __init__(self):
        self.receipts, self.messages, self.cancelled = [], [], []
        self.available = True

    def status(self):
        return {"ready": True}

    def set_session_available(self, value):
        self.available = value

    def set_session_probe(self, probe):
        self.probe = probe

    def sync_persistent(self, messages):
        self.persistent = messages

    def notify(self, title, message, **options):
        self.messages.append((title, message, options))
        return True  # Queue acceptance is deliberately not a receipt.

    def drain_receipts(self):
        values, self.receipts = self.receipts, []
        return values

    def cancel_delivery(self, delivery_id):
        self.cancelled.append(delivery_id)

    def start(self):
        pass

    def stop(self):
        pass


class BrokerClient:
    def __init__(self, broker):
        self.broker, self.requests = broker, []
        self.fail_ack = False

    def connect(self):
        pass

    def poll(self, client_id, after, delivery, **options):
        self.requests.append(options)
        response = self.broker.poll({"client_id": client_id, "after": after, "delivery": delivery,
                                     "protocol": 2, **options})
        if self.fail_ack and options["acks"]:
            self.fail_ack = False
            raise OSError("Lost ACK response")
        return response


class AgentAckTests(unittest.TestCase):
    def setUp(self):
        self.clock = Mock(return_value=10)
        self.broker = SessionBroker(clock=self.clock)
        self.broker.start()
        self.notifier = ReceiptNotifier()
        self.client = BrokerClient(self.broker)
        self.state = "active"
        self.agent = Agent(self.client, self.notifier, clock=self.clock, session_state=lambda: self.state)
        self.broker.publish_event("power_lost", "Corte", "En batería")

    def delivery_id(self):
        return self.notifier.messages[-1][2]["delivery_id"]

    def test_queue_acceptance_is_not_ack_and_receipt_ack_removes_only_after_http_success(self):
        self.agent.step()
        self.agent.step()
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertEqual(self.client.requests[-1]["acks"], [])
        self.notifier.receipts = [{"delivery_id": self.delivery_id(), "outcome": "accepted"}]
        self.client.fail_ack = True
        with self.assertRaises(OSError):
            self.agent.step()
        self.assertEqual(len(self.agent._acks), 1)
        self.agent.step()
        self.assertEqual(self.agent._acks, set())
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertEqual(self.broker.status()["pending_retained"], 0)

    def test_failed_receipt_retries_and_locked_receipt_never_acknowledges(self):
        self.agent.step()
        identifier = self.delivery_id()
        self.notifier.receipts = [{"delivery_id": identifier, "outcome": "failed"}]
        self.agent.step()
        self.assertEqual(len(self.notifier.messages), 2)
        self.state = "locked"
        self.notifier.receipts = [{"delivery_id": identifier, "outcome": "accepted"}]
        self.agent.step()
        self.assertFalse(self.notifier.available)
        self.assertEqual(self.client.requests[-1]["acks"], [])
        self.state = "active"
        self.agent.step()
        self.assertEqual(len(self.notifier.messages), 3)

    def test_unknown_and_disconnected_sessions_defer_and_resume_immediately(self):
        for state in ("unknown", "disconnected", "locked"):
            self.state = state
            self.agent.step()
            self.assertEqual(self.notifier.messages, [])
        self.state = "active"
        self.agent.step()
        self.assertEqual(len(self.notifier.messages), 1)

    def test_lock_between_poll_and_enqueue_defers_message(self):
        states = iter(("active", "locked"))
        self.agent._session_state = lambda: next(states)
        self.agent.step()
        self.assertEqual(self.notifier.messages, [])
        self.agent._session_state = lambda: "active"
        self.agent.step()
        self.assertEqual(len(self.notifier.messages), 1)

    def test_changed_payload_cancels_pending_ticket_and_uses_safe_summary(self):
        self.broker.sync_conditions({}, power_state="battery")
        self.agent.step()
        identifier = self.delivery_id()
        self.broker.sync_conditions({}, power_state="online")
        self.agent.step()
        self.assertEqual(self.notifier.cancelled, [identifier])
        self.assertIn("ya se recuperó", self.notifier.messages[-1][1])

    def test_service_epoch_change_cannot_ack_old_id_or_keep_old_native_ticket(self):
        self.agent.step()
        previous = self.delivery_id()
        replacement = SessionBroker(clock=self.clock)
        replacement.start()
        replacement.publish_event("power_lost", "Nuevo corte", "Batería")
        self.client.broker = replacement
        self.agent.step()
        self.assertNotEqual(previous, self.delivery_id())
        self.assertEqual(self.agent._acks, set())

    def test_local_service_loss_warning_can_deliver_on_active_desktop_after_invalidating_broker(self):
        self.agent.had_connection = True
        self.client.connect = Mock(side_effect=OSError("Unavailable"))
        original_notify = self.notifier.notify
        def notify(*args, **kwargs):
            self.assertTrue(self.notifier.available)
            result = original_notify(*args, **kwargs)
            self.agent.stop_event.set()
            return result
        self.notifier.notify = notify
        self.agent.run()
        self.assertIn("No se puede contactar", self.notifier.messages[-1][1])


class SemanticAlertTests(unittest.TestCase):
    def test_conditions_are_published_without_sticky_popup_setting_and_action_invalidates_countdown(self):
        store = Store(":memory:")
        broker = SessionBroker()
        alerts = AlertManager(store, broker)
        alerts.start()
        try:
            sample = {"timestamp": datetime.now(timezone.utc).isoformat(), "on_battery": True, "low_battery": True}
            alerts.update({"state": "countdown", "countdown_remaining": 20, "action": "shutdown"}, "connected", sample)
            self.assertFalse(alerts.status()["settings"]["persistent_critical"])
            self.assertIn("policy_countdown", broker._conditions)
            self.assertEqual(broker._persistent, {})
            alerts.on_event("policy_action_started", "Ejecutando apagado")
            self.assertNotIn("policy_countdown", broker._conditions)
            alerts.update({}, "stale", sample)
            self.assertEqual(broker._conditions, {})
            self.assertGreater(broker.status()["pending_retained"], 0)
            alerts.update_settings({"enabled": False})
            self.assertEqual(broker.status()["pending_retained"], 0)
        finally:
            alerts.stop()
            store.close()


class NativeQueueIntegrationTests(unittest.TestCase):
    def test_expired_inflight_invalidates_native_ticket_before_retrying_new_context(self):
        clock = Mock(return_value=10)
        broker = SessionBroker(clock=clock)
        broker.start()
        notifier = WindowsNotifier("http://127.0.0.1:8765")
        backend = SimpleNamespace(wake=lambda: True, deliver=Mock(return_value=True), sync_persistent=lambda: None)
        notifier._backend = backend
        notifier._set_ready(True)
        agent = Agent(BrokerClient(broker), notifier, clock=clock, session_state=lambda: "active")
        broker.sync_conditions({}, power_state="battery")
        broker.publish_event("power_lost", "Corte", "En batería")
        agent.step()
        clock.return_value = 16  # Native queue has not drained for six seconds.
        broker.sync_conditions({}, power_state="online")
        agent.step()
        notifier._drain(backend)
        backend.deliver.assert_called_once()
        self.assertIn("ya se recuperó", backend.deliver.call_args.args[1])

    def test_lock_recovery_and_lost_ack_use_native_receipts_without_replaying_old_popup(self):
        broker = SessionBroker()
        broker.start()
        client = BrokerClient(broker)
        notifier = WindowsNotifier("http://127.0.0.1:8765")
        backend = SimpleNamespace(wake=lambda: True, deliver=Mock(return_value=True), sync_persistent=lambda: None)
        notifier._backend = backend
        notifier._set_ready(True)
        state = ["active"]
        agent = Agent(client, notifier, session_state=lambda: state[0])
        broker.sync_conditions({}, power_state="battery")
        broker.publish_event("power_lost", "Corte", "En batería")
        agent.step()
        self.assertEqual(broker.status()["pending_retained"], 1)
        backend.deliver.assert_not_called()
        # Lock races the native queue, without another agent polling first.
        state[0] = "locked"
        notifier._drain(backend)
        backend.deliver.assert_not_called()
        broker.sync_conditions({}, power_state="online")
        state[0] = "active"
        agent.step()
        notifier._drain(backend)
        backend.deliver.assert_called_once()
        self.assertIn("ya se recuperó", backend.deliver.call_args.args[1])
        client.fail_ack = True
        with self.assertRaises(OSError):
            agent.step()
        agent.step()
        notifier._drain(backend)
        backend.deliver.assert_called_once()
        self.assertEqual(broker.status()["pending_retained"], 0)


class SessionProbeTests(unittest.TestCase):
    def probe(self, *, connection=0, flags=1, level=1, size=None, success=True):
        info = _WTSINFOEX()
        info.Level = level
        info.Data.Level1.SessionState = connection
        info.Data.Level1.SessionFlags = flags
        api = Mock()
        def query(_server, session, kind, buffer, returned):
            self.assertEqual((session, kind), (0xFFFFFFFF, 25))
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(info)
            ctypes.cast(returned, ctypes.POINTER(ctypes.c_uint32)).contents.value = ctypes.sizeof(info) if size is None else size
            return success
        api.WTSQuerySessionInformationW.side_effect = query
        with patch("ups_monitor.session_agent.os.name", "nt"), patch.object(ctypes, "WinDLL", return_value=api, create=True):
            result = query_session_state()
        api.WTSFreeMemory.assert_called_once()
        return result

    def test_wts_infoex_alignment_and_session_states(self):
        self.assertEqual(_WTSINFOEX.Data.offset, 8)
        self.assertEqual(self.probe(), "active")
        self.assertEqual(self.probe(flags=0), "locked")
        self.assertEqual(self.probe(connection=4), "disconnected")
        self.assertEqual(self.probe(flags=-1), "unknown")

    def test_failed_short_or_unknown_level_is_fail_closed_and_buffer_freed(self):
        self.assertEqual(self.probe(success=False), "unknown")
        self.assertEqual(self.probe(size=8), "unknown")
        self.assertEqual(self.probe(level=2), "unknown")


if __name__ == "__main__":
    unittest.main()
