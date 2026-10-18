"""Guardian boundaries without starting processes, sockets, HID or native APIs."""
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from ups_monitor import guardian
from ups_monitor import server


class WaitingChild:
    def __init__(self, pid=4321):
        self.pid = pid
        self.code = None
        self.kill = Mock()
        self.terminate = Mock()

    def poll(self):
        return self.code

    def wait(self, timeout):
        if self.code is None:
            raise subprocess.TimeoutExpired("isolated-child", timeout)
        return self.code


class GuardianIntegrationTests(unittest.TestCase):
    def options(self):
        return guardian.parse_args(["--port", "8765", "--source", "native", "--dry-run",
                                    "--observe-hours", "24", "--data-dir",
                                    str(Path(__file__).resolve().parents[1] / ".test-data" / "guardian child with spaces")])

    def supervisor(self, status, *, stop_sender=None):
        ownership = Mock()
        process_factory = Mock(side_effect=AssertionError("No process may be started in this test"))
        sender = stop_sender or Mock()
        instance = guardian.Supervisor(self.options(), process_factory=process_factory,
                                       status_fetcher=Mock(return_value=status), stop_sender=sender,
                                       state_writer=Mock(), ownership_factory=lambda: ownership)
        return instance, process_factory, sender

    def test_fixed_child_command_round_trips_monitor_parser_and_preserves_action_barrier(self):
        options = self.options()
        command = guardian.child_command(options)
        self.assertEqual(command[:3], [sys.executable, "-m", "ups_monitor"])
        self.assertIn(str(options.data_dir), command)
        self.assertIn(" ", str(options.data_dir))
        self.assertEqual(command.count("--dry-run"), 1)
        self.assertNotIn("--enabled", command)
        self.assertNotIn("--shutdown", command)
        ownership = Mock()
        with patch("sys.argv", ["ups_monitor", *command[3:]]), \
             patch.object(server, "MonitorInstanceGuard", return_value=ownership), \
             patch.object(server, "_run_owned_monitor") as owned_runner:
            server.main()
        parsed, actual_ownership = owned_runner.call_args.args
        self.assertTrue(parsed.dry_run)
        self.assertEqual(parsed.source, "native")
        self.assertEqual(parsed.observe_hours, 24)
        self.assertEqual(parsed.data_dir, options.data_dir)
        self.assertIs(actual_ownership, ownership)
        ownership.acquire.assert_called_once_with()

    def test_guardian_argument_errors_reject_remote_urls_credentials_and_unknown_power_flags(self):
        invalid = (["--port", "0"], ["--port", "65536"], ["--observe-hours", "nan"],
                   ["--observe-hours", "169"], ["--source", "unknown"], ["--shutdown"],
                   ["--forza-url", "https://127.0.0.1:15179/Forza"],
                   ["--forza-url", "http://evil.example/Forza"],
                   ["--forza-url", "http://user:password@localhost:15179/Forza"],
                   ["--forza-url", "http://localhost:15179/Forza?token=value"],
                   ["--forza-url", "http://localhost:65536/Forza"])
        for arguments in invalid:
            with self.subTest(arguments=arguments), patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    guardian.parse_args(arguments)
                self.assertEqual(error.exception.code, 2)

    def test_stop_never_posts_to_a_foreign_or_unidentified_local_process(self):
        own = {"meta": {"name": "Elipro UPS"}, "health": {"process": {"pid": 4321}}, "csrf_token": "a" * 32}
        invalid = [None, [], {}, {**own, "meta": {"name": "Other application"}},
                   {**own, "health": {"process": {"pid": 9876}}},
                   {**own, "health": {"process": {"pid": True}}},
                   {**own, "health": {"process": {"pid": "4321"}}},
                   {**own, "csrf_token": ""}, {**own, "csrf_token": "x" * 257}]
        for status in invalid:
            with self.subTest(status=status):
                supervisor, factory, sender = self.supervisor(status)
                child = WaitingChild()
                supervisor.child = child
                self.assertFalse(supervisor.stop())
                sender.assert_not_called()
                factory.assert_not_called()
                child.kill.assert_not_called()
                child.terminate.assert_not_called()
                self.assertTrue(supervisor.stop_requested.is_set())
                self.assertEqual(supervisor.state["state"], "failed")
                self.assertEqual(supervisor.state["child_pid"], child.pid)

    def test_matching_child_gets_only_graceful_api_stop_and_token_is_never_persisted(self):
        private_token = "DO-NOT-PERSIST-THIS-CSRF-TOKEN"
        status = {"meta": {"name": "Elipro UPS"}, "health": {"process": {"pid": 4321}}, "csrf_token": private_token}
        child = WaitingChild()
        def stop_sender(port, token):
            self.assertEqual((port, token), (8765, private_token))
            child.code = 0
            return True
        sender = Mock(side_effect=stop_sender)
        supervisor, factory, _ = self.supervisor(status, stop_sender=sender)
        supervisor.child = child
        self.assertTrue(supervisor.stop())
        sender.assert_called_once_with(8765, private_token)
        factory.assert_not_called()
        child.kill.assert_not_called()
        child.terminate.assert_not_called()
        self.assertEqual(supervisor.state["state"], "stopped")
        self.assertNotIn(private_token, json.dumps(supervisor.state))
        for call in supervisor.state_writer.call_args_list:
            self.assertNotIn(private_token, json.dumps(call.args[1]))

    def test_status_fetch_is_fixed_loopback_bounded_json_without_network(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b'{"meta":{"name":"Elipro UPS"}}')
        with patch.object(guardian, "_opener", return_value=opener):
            self.assertEqual(guardian.fetch_status(8765)["meta"]["name"], "Elipro UPS")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/api/status")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 2)
        for payload in (b"[]", b"null", b"not-json", b"x" * 262145):
            opener.open.return_value = io.BytesIO(payload)
            with self.subTest(size=len(payload)), patch.object(guardian, "_opener", return_value=opener):
                with self.assertRaises(ValueError):
                    guardian.fetch_status(8765)

    def test_stop_request_is_fixed_local_post_and_requires_explicit_acknowledgement(self):
        opener = Mock()
        for payload, expected in ((b'{"stopped":true}', True), (b'{"stopped":false}', False),
                                  (b"[]", False), (b"x" * 4097, False)):
            opener.open.return_value = io.BytesIO(payload)
            with self.subTest(size=len(payload)), patch.object(guardian, "_opener", return_value=opener):
                self.assertEqual(guardian.send_stop(8765, "local-token"), expected)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:8765/api/service/stop")
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.data, b"{}")
            self.assertEqual(request.get_header("X-ups-token"), "local-token")

    def test_local_http_redirects_cannot_forward_status_or_stop_credentials(self):
        request = Mock(full_url="http://127.0.0.1:8765/api/service/stop")
        with self.assertRaises(HTTPError):
            guardian._NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://evil.example/")


if __name__ == "__main__":
    unittest.main()
