"""Temporary cancellation-test permission; fake token/SCM and no USB/power IO."""
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from ups_monitor import ups_test_authorization as authorization


class AuthorizationFixture(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parents[1] / ".test-data"
        test_root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(self.directory.cleanup)
        self.data_dir = Path(self.directory.name).resolve()
        self.auth_path = self.data_dir / authorization.AUTHORIZATION_FILENAME
        self.journal_path = self.data_dir / authorization.JOURNAL_FILENAME
        self.now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)

    def permission(self, **changes):
        return {"schema_version": 1, "owner": authorization.AUTHORIZATION_OWNER,
                "authorization_id": "0123456789abcdef0123456789abcdef",
                "issued_at": self.now.isoformat(),
                "expires_at": (self.now + timedelta(minutes=15)).isoformat(), **changes}

    def write_permission(self, **changes):
        value = self.permission(**changes)
        self.auth_path.write_text(json.dumps(value), encoding="utf-8")
        return value

    def journal(self, state="finished", **changes):
        value = {"schema_version": 1, "owner": authorization.JOURNAL_OWNER, "state": state, **changes}
        self.journal_path.write_text(json.dumps(value), encoding="utf-8")
        return value


class ReadAuthorizationTests(AuthorizationFixture):
    def test_reads_utc_permission_and_honors_exact_expiry_boundary(self):
        expected = self.write_permission()
        self.assertEqual(authorization.read_test_authorization(self.auth_path, now=self.now), expected)
        with self.assertRaises(ValueError):
            authorization.read_test_authorization(self.auth_path, now=self.now + timedelta(minutes=15))
        self.assertEqual(authorization.read_test_authorization(
            self.auth_path, now=self.now + timedelta(days=1), allow_expired=True), expected)

    def test_types_owner_schema_uuid_and_unknown_fields_are_strict(self):
        for changes in ({"schema_version": True}, {"schema_version": 2}, {"owner": "foreign"},
                        {"authorization_id": "A" * 32}, {"authorization_id": "0" * 31},
                        {"authorization_id": 123}, {"issued_at": None}, {"expires_at": 123},
                        {"unexpected": True}):
            with self.subTest(changes=changes):
                self.write_permission(**changes)
                with self.assertRaises(ValueError):
                    authorization.read_test_authorization(self.auth_path, now=self.now, allow_expired=True)
        value = self.permission()
        del value["owner"]
        self.auth_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(ValueError):
            authorization.read_test_authorization(self.auth_path, now=self.now)

    def test_rejects_missing_malformed_oversized_nonobject_and_duplicate_json(self):
        with self.assertRaises(ValueError):
            authorization.read_test_authorization(self.auth_path, now=self.now)
        values = (b"{", b"[]", b"null", b"\xff", b" " * 4097,
                  b'{"owner":"first","owner":"second"}', b"[" * 1100 + b"]" * 1100)
        for raw in values:
            with self.subTest(raw=raw[:30]):
                self.auth_path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    authorization.read_test_authorization(self.auth_path, now=self.now)

    def test_dates_require_utc_zone_positive_duration_and_at_most_fifteen_minutes(self):
        for changes in ({"issued_at": "2026-10-18T12:00:00"}, {"expires_at": "not a date"},
                        {"issued_at": "2026-10-18T07:00:00-05:00"},
                        {"expires_at": self.now.isoformat()},
                        {"expires_at": (self.now - timedelta(seconds=1)).isoformat()},
                        {"expires_at": (self.now + timedelta(seconds=901)).isoformat()},
                        {"issued_at": (self.now + timedelta(seconds=1)).isoformat()}):
            with self.subTest(changes=changes):
                self.write_permission(**changes)
                with self.assertRaises(ValueError):
                    authorization.read_test_authorization(self.auth_path, now=self.now, allow_expired=True)
        self.write_permission(issued_at="2026-10-18T12:00:00Z", expires_at="2026-10-18T12:05:00Z")
        self.assertIsInstance(authorization.read_test_authorization(self.auth_path, now=self.now), dict)

    def test_reader_rejects_naive_clock_or_nonboolean_expiry_override(self):
        self.write_permission()
        for kwargs in ({"now": datetime(2026, 10, 18)}, {"now": 1}, {"allow_expired": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                authorization.read_test_authorization(self.auth_path, **kwargs)


class WorkerAuthorizationTests(AuthorizationFixture):
    def registered(self, *, arguments=None, status=None, owned=True, manager=1, handle=2, query_error=None):
        service = authorization.service
        api = Mock()
        api.OpenSCManagerW.return_value, api.OpenServiceW.return_value = manager, handle
        vector = arguments if arguments is not None else [*service.child_command("service", "run"),
                                                         "--data-dir", str(self.data_dir), "--port", "8765",
                                                         "--allow-power-actions"]
        with patch.object(authorization.os, "name", "nt"), \
                patch.object(service, "_apis", return_value=api), \
                patch.object(service, "_query", return_value=status or {"installed": True, "state": "running"},
                             side_effect=query_error), \
                patch.object(service, "_configuration", return_value={"binary_path": subprocess.list2cmdline(vector)}), \
                patch.object(service, "_owned_binary", return_value=owned), \
                patch.object(authorization, "_command_line_args", return_value=vector):
            try:
                result = authorization._registered_data_dir()
            except (OSError, ValueError) as exc:
                result = exc
        for name in ("CreateServiceW", "ChangeServiceConfigW", "ChangeServiceConfig2W", "StartServiceW",
                     "ControlService", "DeleteService"):
            getattr(api, name).assert_not_called()
        return result, api

    def test_registered_native_windows_permission_does_not_require_production_ups_gate(self):
        result, api = self.registered()
        self.assertEqual(result, self.data_dir)
        api.OpenSCManagerW.assert_called_once_with(None, None, authorization.service.SC_MANAGER_CONNECT)
        api.OpenServiceW.assert_called_once_with(1, authorization.service.NAME,
                                                authorization.service.SERVICE_QUERY_STATUS | authorization.service.SERVICE_QUERY_CONFIG)
        self.assertEqual([call.args[0] for call in api.CloseServiceHandle.call_args_list], [2, 1])

    def test_registration_rejects_absent_nonrunning_foreign_or_disallowed_modes(self):
        for status in ({"installed": False, "state": "absent"}, {"installed": 1, "state": "running"},
                       {"installed": True, "state": "stop_pending"}, {"installed": True, "state": "stopped"}):
            with self.subTest(status=status):
                self.assertIsInstance(self.registered(status=status)[0], ValueError)
        self.assertIsInstance(self.registered(owned=False)[0], ValueError)
        base = ["Elipro.exe", "service", "run", "--data-dir", str(self.data_dir)]
        for flags in ([], ["--allow-ups-poweroff"], ["--allow-power-actions", "--self-test"],
                      ["--allow-power-actions", "--simulate"], ["--allow-power-actions", "--dry-run"]):
            with self.subTest(flags=flags):
                self.assertIsInstance(self.registered(arguments=base + flags)[0], ValueError)

    def test_registration_requires_one_absolute_existing_data_directory(self):
        base = ["Elipro.exe", "service", "run", "--allow-power-actions"]
        for tail in ([], ["--data-dir"], ["--data-dir", "relative"],
                     ["--data-dir", str(self.data_dir / "missing")],
                     ["--data-dir", str(self.data_dir), "--data-dir", str(self.data_dir)],
                     ["--data-dir=" + str(self.data_dir)]):
            with self.subTest(tail=tail):
                self.assertIsInstance(self.registered(arguments=base + tail)[0], ValueError)
        nested = self.data_dir / "folder --simulate and spaces"
        nested.mkdir()
        self.assertEqual(self.registered(arguments=base + ["--data-dir", str(nested)])[0], nested)

    def test_scm_failures_deny_and_close_only_acquired_handles(self):
        result, api = self.registered(manager=0)
        self.assertIsInstance(result, OSError)
        api.CloseServiceHandle.assert_not_called()
        result, api = self.registered(handle=0)
        self.assertIsInstance(result, ValueError)
        api.CloseServiceHandle.assert_called_once_with(1)
        result, api = self.registered(query_error=OSError("fake query error"))
        self.assertIsInstance(result, OSError)
        self.assertEqual([call.args[0] for call in api.CloseServiceHandle.call_args_list], [2, 1])

    def test_worker_requires_real_system_identity_before_any_service_lookup(self):
        with patch.object(authorization.service, "_is_local_system", return_value=False), \
                patch.object(authorization, "_registered_data_dir") as registered, \
                patch.dict(authorization.os.environ, {"USERNAME": "SYSTEM"}):
            with self.assertRaises(ValueError):
                authorization.authorize_test_worker(str(self.auth_path), str(self.journal_path))
        registered.assert_not_called()

    def test_worker_accepts_structurally_valid_expired_permission_for_owned_cancellation(self):
        self.write_permission(issued_at="2000-01-01T00:00:00Z", expires_at="2000-01-01T00:15:00Z")
        with patch.object(authorization.service, "_is_local_system", return_value=True), \
                patch.object(authorization, "_registered_data_dir", return_value=self.data_dir):
            self.assertIs(authorization.authorize_test_worker(str(self.auth_path), str(self.journal_path)), True)
            self.auth_path.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                authorization.authorize_test_worker(str(self.auth_path), str(self.journal_path))

    def test_worker_rejects_foreign_parent_relative_paths_and_wrong_filenames(self):
        self.write_permission()
        with patch.object(authorization.service, "_is_local_system", return_value=True), \
                patch.object(authorization, "_registered_data_dir", return_value=self.data_dir):
            for auth_path, journal_path in ((self.data_dir.parent / self.auth_path.name, self.journal_path),
                                            (self.auth_path, self.data_dir.parent / self.journal_path.name),
                                            (Path(self.auth_path.name), self.journal_path),
                                            (self.data_dir / "wrong.json", self.journal_path),
                                            (self.auth_path, self.data_dir / "wrong.json")):
                with self.subTest(auth_path=auth_path, journal_path=journal_path), self.assertRaises(ValueError):
                    authorization.authorize_test_worker(str(auth_path), str(journal_path))


class AuthorizationCliTests(AuthorizationFixture):
    def run_cli(self, argv=None, *, admin=True, platform="nt"):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(authorization.os, "name", platform), \
                patch.object(authorization, "_is_admin", return_value=admin), \
                patch.object(authorization, "_registered_data_dir", return_value=self.data_dir) as registered, \
                patch.object(authorization, "_now_utc", return_value=self.now), \
                patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            result = authorization.main(argv)
        return result, stdout.getvalue(), stderr.getvalue(), registered

    def test_cli_writes_atomic_fsynced_fifteen_minute_permission_without_printing_id(self):
        real_fsync, real_replace = authorization.os.fsync, authorization.os.replace
        with patch.object(authorization.os, "fsync", wraps=real_fsync) as fsync, \
                patch.object(authorization.os, "replace", wraps=real_replace) as replace:
            result, output, error, _ = self.run_cli([])
        self.assertEqual((result, error), (0, ""))
        value = authorization.read_test_authorization(self.auth_path, now=self.now)
        self.assertEqual(set(json.loads(output)), {"path", "expires_at"})
        self.assertNotIn(value["authorization_id"], output)
        self.assertEqual(json.loads(output)["path"], str(self.auth_path))
        self.assertEqual(value["expires_at"], "2026-10-18T12:15:00Z")
        self.assertGreaterEqual(fsync.call_count, 2)
        replace.assert_called_once()
        self.assertEqual(Path(replace.call_args.args[0]).parent, self.data_dir)
        self.assertEqual(replace.call_args.args[1], self.auth_path)
        self.assertEqual(list(self.data_dir.glob("*.tmp")), [])

    def test_cli_custom_duration_and_finished_journal_allow_renewal(self):
        self.write_permission()
        self.journal()
        result, _, error, _ = self.run_cli(["--minutes", "5"])
        self.assertEqual((result, error), (0, ""))
        self.assertEqual(authorization.read_test_authorization(self.auth_path, now=self.now)["expires_at"],
                         "2026-10-18T12:05:00Z")

    def test_cli_requires_windows_and_actual_elevated_token_before_scm(self):
        for options in ({"admin": False}, {"platform": "posix"}):
            with self.subTest(options=options):
                result, output, error, registered = self.run_cli([], **options)
                self.assertEqual((result, output), (1, ""))
                self.assertIn("administrador", error)
                registered.assert_not_called()
                self.assertFalse(self.auth_path.exists())

    def test_cli_rejects_nonterminal_and_invalid_journals_without_overwriting_permission(self):
        self.write_permission()
        before = self.auth_path.read_bytes()
        for state in authorization._OPERATION_STATES - {"finished"} | {"cancelled", "unknown"}:
            with self.subTest(state=state):
                self.journal(state)
                result, output, _, _ = self.run_cli([])
                self.assertEqual((result, output), (1, ""))
                self.assertEqual(self.auth_path.read_bytes(), before)
        for raw in ('{"owner":"foreign","state":"finished"}', "{", "[]", " " * 65537):
            with self.subTest(raw=raw[:40]):
                self.journal_path.write_text(raw, encoding="utf-8")
                self.assertEqual(self.run_cli([])[0], 1)
                self.assertEqual(self.auth_path.read_bytes(), before)

    def test_atomic_writer_rechecks_journal_before_replace_and_cleans_temp_on_failure(self):
        self.write_permission()
        before = self.auth_path.read_bytes()
        with patch.object(authorization, "_require_finished_journal", side_effect=[None, ValueError("pending")]), \
                patch.object(authorization.os, "replace") as replace:
            self.assertEqual(self.run_cli([])[0], 1)
        replace.assert_not_called()
        self.assertEqual(self.auth_path.read_bytes(), before)
        self.assertEqual(list(self.data_dir.glob("*.tmp")), [])
        with patch.object(authorization.os, "replace", side_effect=OSError("fake storage failure")):
            self.assertEqual(self.run_cli([])[0], 1)
        self.assertEqual(self.auth_path.read_bytes(), before)
        self.assertEqual(list(self.data_dir.glob("*.tmp")), [])

    def test_cli_rejects_arbitrary_paths_and_out_of_range_minutes(self):
        for arguments in (["--data-dir", str(self.data_dir)], ["--minutes", "4"], ["--minutes", "16"]):
            with self.subTest(arguments=arguments), patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as caught:
                authorization.main(arguments)
            self.assertEqual(caught.exception.code, 2)
        self.assertFalse(self.auth_path.exists())

    def test_admin_check_uses_token_api_and_fails_closed(self):
        shell = Mock()
        for result in (0, 1):
            shell.IsUserAnAdmin.return_value = result
            with patch.object(authorization.os, "name", "nt"), \
                    patch.object(authorization.ctypes, "WinDLL", return_value=shell):
                self.assertIs(authorization._is_admin(), bool(result))
        with patch.object(authorization.os, "name", "nt"), \
                patch.object(authorization.ctypes, "WinDLL", side_effect=OSError("fake API failure")):
            self.assertFalse(authorization._is_admin())
        with patch.object(authorization.os, "name", "posix"), patch.object(authorization.ctypes, "WinDLL") as dll:
            self.assertFalse(authorization._is_admin())
        dll.assert_not_called()


if __name__ == "__main__":
    unittest.main()
