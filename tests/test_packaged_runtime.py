"""Packaging contracts only: never start a real monitor, USB worker or power action."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from ups_monitor import entrypoint, guardian, hid_windows, runtime


HID_PATH = r"\\?\hid#vid_0665&pid_5161#unit#{4d1e55b2-f16f-11cf-88cb-001111000030}"


class PackagedRuntimeTests(unittest.TestCase):
    def test_source_commands_preserve_interpreter_module_contract(self):
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(runtime.child_command("monitor", "--dry-run"),
                             [sys.executable, "-m", "ups_monitor", "--dry-run"])
            self.assertEqual(runtime.child_command("qs-worker", HID_PATH),
                             [sys.executable, "-m", "ups_monitor.hid_windows", "--qs-worker", HID_PATH])
            self.assertEqual(runtime.child_command("battery-worker", HID_PATH, "BL"),
                             [sys.executable, "-m", "ups_monitor.hid_windows", "--battery-worker", HID_PATH, "BL"])
            self.assertEqual(runtime.child_command("battery-diagnostic", "--help"),
                             [sys.executable, "-m", "ups_monitor.battery_diagnostics", "--help"])

    def test_frozen_commands_cannot_recurse_through_python_m(self):
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", r"C:\Apps\Elipro.exe"):
            self.assertEqual(runtime.child_command("monitor", "--dry-run"),
                             [r"C:\Apps\Elipro.exe", "monitor", "--dry-run"])
            self.assertEqual(runtime.child_command("qs-worker", HID_PATH),
                             [r"C:\Apps\Elipro.exe", "qs-worker", HID_PATH])
            self.assertEqual(runtime.child_command("battery-worker", HID_PATH, "BT"),
                             [r"C:\Apps\Elipro.exe", "battery-worker", HID_PATH, "BT"])
            self.assertEqual(runtime.child_command("battery-diagnostic", "--help"),
                             [r"C:\Apps\Elipro.exe", "battery-diagnostic", "--help"])
            with self.assertRaises(ValueError):
                runtime.child_command("arbitrary.module")

    def test_frozen_defaults_do_not_write_beside_executable(self):
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "_MEIPASS", r"C:\Program Files\Elipro\_internal", create=True), \
                patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\Test\AppData\Local"}):
            self.assertEqual(runtime.default_data_dir(), Path(os.environ["LOCALAPPDATA"]) / "Elipro UPS" / "data")
            options = guardian.parse_args(["--dry-run"])
            self.assertEqual(options.data_dir, runtime.default_data_dir().resolve())
            command = guardian.child_command(options)
            self.assertEqual(command[1], "monitor")
            self.assertIn("--dry-run", command)
            self.assertNotIn("-m", command)

    def test_frozen_hid_uses_safe_internal_role_with_original_bounds(self):
        with patch.object(sys, "frozen", True, create=True), \
                patch.dict(hid_windows._cooldown_until, {}, clear=True), \
                patch("ups_monitor.hid_windows._run_bounded_worker") as run:
            run.return_value = Mock(returncode=0, stdout=b"#sample\r", stderr=b"")
            self.assertEqual(hid_windows.StatusTransport(HID_PATH).query_status(), b"#sample\r")
            self.assertEqual(run.call_args.args[0], [sys.executable, "qs-worker", HID_PATH])
            self.assertEqual(run.call_args.args[1], HID_PATH.casefold())

    def test_smoke_checks_resources_without_usb_or_process_or_data_creation(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            (root / "static").mkdir()
            for name in ("index.html", "app.js", "style.css"):
                (root / "static" / name).write_text("safe resource", encoding="utf-8")
            with patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "_MEIPASS", str(root), create=True), \
                    patch.dict(os.environ, {"LOCALAPPDATA": str(root.parent / "localdata")}), \
                    patch("subprocess.Popen") as popen, patch("subprocess.run") as run, \
                    patch("ups_monitor.hid_windows._apis") as apis:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(entrypoint.main(["smoke-test"]), 0)
                payload = json.loads(output.getvalue())
                self.assertTrue(payload["ok"])
                self.assertTrue(payload["data_outside_bundle"])
                self.assertEqual(payload["commands"]["qs-worker"], ["qs-worker"])
                popen.assert_not_called()
                run.assert_not_called()
                apis.assert_not_called()
                self.assertEqual([path.name for path in root.iterdir()], ["static"])
                (root / "static" / "app.js").unlink()
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(entrypoint.main(["smoke-test"]), 1)

    def test_dispatch_keeps_roles_separate(self):
        with patch("ups_monitor.server.main") as monitor, patch("ups_monitor.guardian.main") as supervisor:
            self.assertEqual(entrypoint.main(["monitor", "--dry-run"]), 0)
            monitor.assert_called_once_with(["--dry-run"])
            supervisor.assert_not_called()
            self.assertEqual(entrypoint.main(["guardian", "--dry-run"]), 0)
            supervisor.assert_called_once_with(["--dry-run"])
        with patch("ups_monitor.hid_windows.worker_main", return_value=1) as worker:
            self.assertEqual(entrypoint.main(["qs-worker", HID_PATH]), 1)
            worker.assert_called_once_with([HID_PATH])
        with patch("ups_monitor.battery_diagnostics.main", return_value=0) as diagnostic, \
                patch("ups_monitor.hid_windows._apis") as apis:
            self.assertEqual(entrypoint.main(["battery-diagnostic", "--help"]), 0)
            diagnostic.assert_called_once_with(["--help"])
            apis.assert_not_called()
        with patch("ups_monitor.hid_windows.battery_worker_main", return_value=1) as worker:
            self.assertEqual(entrypoint.main(["battery-worker", HID_PATH, "BL"]), 1)
            worker.assert_called_once_with([HID_PATH, "BL"])

    def test_worker_rejects_missing_or_extra_arguments_before_hid_access(self):
        with patch("ups_monitor.hid_windows._query_status_once") as query:
            for arguments in ([], [HID_PATH, "unexpected"]):
                with self.assertRaises(SystemExit):
                    hid_windows.worker_main(arguments)
            query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
