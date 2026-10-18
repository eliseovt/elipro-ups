"""Guardian lifecycle using fake children; no process, USB or power command runs."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from ups_monitor import guardian, instance_guard
from ups_monitor.guardian import Supervisor, parse_args, request_parent_stop
from ups_monitor.instance_guard import AlreadyRunningError, GuardianInstanceGuard, MonitorInstanceGuard


class Clock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class Process:
    def __init__(self, code=None, *, pid=20001, wait=None):
        self.code, self.pid, self.behavior = code, pid, wait
        self.waits = []
        self.terminate, self.kill = Mock(), Mock()

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.behavior is not None:
            return self.behavior(self, timeout)
        if self.code is None:
            raise subprocess.TimeoutExpired("simulated child", timeout)
        return self.code


class GuardianTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.options = parse_args(["--source", "native", "--dry-run", "--data-dir", self.directory.name])
        self.clock, self.owner = Clock(), Mock()
        self.writer = Mock()
        self.factory = Mock()
        self.status = Mock()
        self.sender = Mock(return_value=True)

    def tearDown(self):
        self.directory.cleanup()

    def supervisor(self):
        return Supervisor(self.options, process_factory=self.factory, clock=self.clock, sleep=self.clock.sleep,
                          status_fetcher=self.status, stop_sender=self.sender, state_writer=self.writer,
                          ownership_factory=lambda: self.owner)

    def stop_marker(self, supervisor, **updates):
        value = {"guardian_pid": os.getpid(), "session_id": supervisor.session_id, **updates}
        supervisor.stop_path.write_text(json.dumps(value), encoding="utf-8")

    def test_crash_waits_usb_cooldown_then_restarts_exactly_once(self):
        self.factory.side_effect = [Process(-1), Process(0, pid=20002)]
        supervisor = self.supervisor()
        self.assertEqual(supervisor.run(), 0)
        self.assertEqual(self.factory.call_count, 2)
        self.assertAlmostEqual(sum(self.clock.sleeps), 5)
        self.assertEqual(supervisor.state["restarts"], 1)
        self.assertEqual(supervisor.state["state"], "stopped")
        self.owner.acquire.assert_called_once()
        self.owner.close.assert_called_once()
        for call in self.factory.call_args_list:
            self.assertIn("--dry-run", call.args[0])
            self.assertEqual(call.args[0][1:3], ["-m", "ups_monitor"])
            self.assertEqual(call.kwargs["env"]["ELIPRO_GUARDIAN_PID"], str(os.getpid()))
            self.assertEqual(call.kwargs["env"]["ELIPRO_GUARDIAN_SESSION"], supervisor.session_id)
        self.assertEqual([call.kwargs["env"]["ELIPRO_GUARDIAN_RESTARTS"] for call in self.factory.call_args_list], ["0", "1"])

    def test_five_crashes_within_window_fail_closed_after_bounded_backoff(self):
        self.factory.side_effect = [Process(-1, pid=20000 + index) for index in range(5)]
        supervisor = self.supervisor()
        self.assertEqual(supervisor.run(), 3)
        self.assertEqual(self.factory.call_count, 5)
        self.assertAlmostEqual(sum(self.clock.sleeps), 5 + 10 + 20 + 30)
        self.assertEqual(supervisor.state["restarts"], 4)
        self.assertEqual(supervisor.state["state"], "failed")
        self.assertIsNone(supervisor.state["next_retry_seconds"])
        self.owner.close.assert_called_once()

    def test_stable_runtime_resets_flap_budget_and_retry_delay(self):
        def stable_exit(process, _timeout):
            self.clock.value += 301
            process.code = -1
            return process.code

        self.factory.side_effect = ([Process(-1) for _ in range(4)]
                                    + [Process(wait=stable_exit), Process(0)])
        supervisor = self.supervisor()
        self.assertEqual(supervisor.run(), 0)
        self.assertEqual(self.factory.call_count, 6)
        self.assertAlmostEqual(sum(self.clock.sleeps), 70)

    def test_failed_process_creation_is_bounded_and_never_fakes_a_running_child(self):
        self.factory.side_effect = OSError("private path must not enter state")
        supervisor = self.supervisor()
        self.assertEqual(supervisor.run(), 3)
        self.assertEqual(self.factory.call_count, 5)
        self.assertIsNone(supervisor.child)
        self.assertIsNone(supervisor.state["child_pid"])
        self.assertNotIn("private path", json.dumps(supervisor.state))

    def test_metadata_failure_does_not_stop_or_replace_a_healthy_child(self):
        self.writer.side_effect = OSError("disk full")
        self.factory.return_value = Process(0)
        self.assertEqual(self.supervisor().run(), 0)
        self.factory.assert_called_once()

    def test_second_guardian_is_rejected_before_metadata_or_process_creation(self):
        self.owner.acquire.side_effect = AlreadyRunningError("Ya hay un supervisor")
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(self.supervisor().run(), 2)
        self.writer.assert_not_called()
        self.factory.assert_not_called()
        self.owner.close.assert_not_called()

    def test_valid_stop_marker_during_backoff_prevents_reopening_monitor(self):
        self.factory.return_value = Process(-1)
        supervisor = self.supervisor()
        original_sleep = supervisor.sleep
        def stop_during_sleep(seconds):
            original_sleep(seconds)
            self.stop_marker(supervisor)
        supervisor.sleep = stop_during_sleep
        self.assertEqual(supervisor.run(), 0)
        self.factory.assert_called_once()
        self.assertLess(sum(self.clock.sleeps), 5)
        self.assertEqual(supervisor.state["state"], "stopped")

    def test_stale_stop_marker_from_another_session_is_ignored(self):
        self.factory.side_effect = [Process(-1), Process(0)]
        supervisor = self.supervisor()
        self.stop_marker(supervisor, session_id="old-session")
        self.assertEqual(supervisor.run(), 0)
        self.assertEqual(self.factory.call_count, 2)
        self.assertAlmostEqual(sum(self.clock.sleeps), 5)

    def test_keyboard_interrupt_gracefully_stops_only_verified_child(self):
        calls = []
        def interrupt_once(process, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise KeyboardInterrupt()
            return process.code
        process = Process(wait=interrupt_once)
        self.factory.return_value = process
        self.status.return_value = {"meta": {"name": "Elipro UPS"},
                                    "health": {"process": {"pid": process.pid}}, "csrf_token": "x" * 32}
        self.sender.side_effect = lambda *_args: setattr(process, "code", 0) or True
        supervisor = self.supervisor()
        self.assertEqual(supervisor.run(), 0)
        self.sender.assert_called_once_with(self.options.port, "x" * 32)
        self.assertIn(15, process.waits)
        process.kill.assert_not_called()
        process.terminate.assert_not_called()
        self.owner.close.assert_called_once()

    def test_live_child_after_shutdown_timeout_never_gets_replaced_or_killed(self):
        calls = []
        def interrupt_then_timeout(_process, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise KeyboardInterrupt()
            raise subprocess.TimeoutExpired("simulated child", timeout)
        process = Process(wait=interrupt_then_timeout)
        self.factory.return_value = process
        self.status.return_value = {"meta": {"name": "Elipro UPS"},
                                    "health": {"process": {"pid": process.pid + 1}}, "csrf_token": "x" * 32}
        supervisor = self.supervisor()
        self.assertEqual(supervisor.run(), 3)
        self.factory.assert_called_once()
        self.sender.assert_not_called()
        process.kill.assert_not_called()
        process.terminate.assert_not_called()
        self.owner.close.assert_not_called()
        self.assertEqual(supervisor.state["state"], "failed")

    def test_process_handle_error_is_not_mistaken_for_confirmed_child_exit(self):
        process = Process(wait=lambda *_args: (_ for _ in ()).throw(OSError("broken wait handle")))
        self.factory.return_value = process
        supervisor = self.supervisor()
        self.assertEqual(supervisor.run(), 3)
        self.factory.assert_called_once()
        self.owner.close.assert_not_called()
        self.assertEqual(supervisor.state["child_pid"], process.pid)

    def test_parent_stop_helper_requires_matching_environment_pid_session_and_port(self):
        session = "a" * 32
        state = {"pid": 34567, "child_pid": os.getpid(), "port": 8765, "session_id": session}
        directory = Path(self.directory.name)
        (directory / "guardian.json").write_text(json.dumps(state), encoding="utf-8")
        with patch.dict(os.environ, {"ELIPRO_GUARDIAN_PID": "34567", "ELIPRO_GUARDIAN_SESSION": session}):
            self.assertFalse(request_parent_stop(directory, 9999))
            self.assertFalse((directory / "guardian.stop").exists())
            self.assertTrue(request_parent_stop(directory, 8765))
            marker = json.loads((directory / "guardian.stop").read_text(encoding="utf-8"))
            self.assertEqual(marker, {"guardian_pid": 34567, "session_id": session})
            state["child_pid"] += 1
            (directory / "guardian.json").write_text(json.dumps(state), encoding="utf-8")
            self.assertFalse(request_parent_stop(directory, 8765))

    def test_stop_retries_own_identity_when_child_http_is_still_starting(self):
        process = Process()
        self.status.side_effect = [OSError("HTTP not ready"),
                                  {"meta": {"name": "Elipro UPS"},
                                   "health": {"process": {"pid": process.pid}}, "csrf_token": "x" * 32}]
        self.sender.side_effect = lambda *_args: setattr(process, "code", 0) or True
        supervisor = self.supervisor()
        supervisor.child = process
        self.assertTrue(supervisor.stop())
        self.assertEqual(self.status.call_count, 2)
        self.sender.assert_called_once_with(self.options.port, "x" * 32)
        self.factory.assert_not_called()
        process.kill.assert_not_called()
        self.assertEqual(supervisor.state["state"], "stopped")


class GuardianOwnershipTests(unittest.TestCase):
    def tearDown(self):
        instance_guard._process_owner = None
        instance_guard._guardian_process_owner = None

    def test_guardian_and_monitor_own_different_global_mutexes(self):
        kernel = Mock()
        kernel.CreateMutexW.side_effect = [101, 102]
        kernel.WaitForSingleObject.return_value = instance_guard.WAIT_OBJECT_0
        kernel.ReleaseMutex.return_value = kernel.CloseHandle.return_value = True
        with patch.object(instance_guard.os, "name", "nt"), \
             patch.object(instance_guard, "_load_kernel32", return_value=kernel):
            with GuardianInstanceGuard():
                with self.assertRaises(AlreadyRunningError):
                    GuardianInstanceGuard().acquire()
                with MonitorInstanceGuard():
                    names = [call.args[2] for call in kernel.CreateMutexW.call_args_list]
                    self.assertEqual(names, ["Global\\EliproUPS.Guardian", "Global\\EliproUPS.Monitor"])
        self.assertEqual(kernel.ReleaseMutex.call_count, 2)


if __name__ == "__main__":
    unittest.main()
