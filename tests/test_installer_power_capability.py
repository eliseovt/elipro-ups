"""Upgrade capability contracts with fake status output, never local SCM or USB."""
import base64
import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "packaging" / "windows" / "Read-ServicePowerCapability.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def service_status(**values):
    return {"name": "EliproUPS", "installed": True, "state": "running", "self_test": False,
            "simulation": False, "allow_power_actions": False, "allow_ups_poweroff": False, **values}


@unittest.skipUnless(POWERSHELL, "PowerShell is needed to execute the installer helper contracts")
class InstallerPowerCapabilityTests(unittest.TestCase):
    def execute(self, source):
        script = "$ErrorActionPreference='Stop'\nSet-StrictMode -Version Latest\n"
        script += ". " + ps_literal(HELPER) + "\n"
        script += source
        result = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive",
                                 "-ExecutionPolicy", "Bypass", "-Command", script],
                                capture_output=True, text=True, timeout=20, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout.strip())

    def decisions(self, statuses):
        payloads = [json.dumps(value) if not isinstance(value, str) else value for value in statuses]
        script = "$payloads=@(" + ",".join(ps_literal(value) for value in payloads) + ")\n"
        script += """
$results=@()
foreach($payload in $payloads) {
    $executor={param($program) [pscustomobject]@{ExitCode=0;StdOut=$payload}}.GetNewClosure()
    try {$results+=@{capabilities=(Get-EliproUpgradePowerCapabilities -Executor $executor);failed=$false}}
    catch {$results+=@{capabilities=$null;failed=$true}}
}
@{results=$results}|ConvertTo-Json -Depth 4 -Compress
"""
        return self.execute(script)["results"]

    def test_preserves_only_an_existing_explicit_normal_capability(self):
        legacy_allowed, legacy_blocked = service_status(allow_power_actions=True), service_status()
        legacy_allowed.pop("allow_ups_poweroff")
        legacy_blocked.pop("allow_ups_poweroff")
        results = self.decisions([
            service_status(allow_power_actions=True),
            service_status(allow_power_actions=True, allow_ups_poweroff=True),
            service_status(),
            service_status(installed=False, state="absent"),
            service_status(self_test=True),
            service_status(simulation=True),
            service_status(allow_power_actions=True, allow_ups_poweroff=True, state="stopped"),
            legacy_allowed, legacy_blocked,
        ])
        self.assertEqual([(row["capabilities"]["allow_power_actions"], row["capabilities"]["allow_ups_poweroff"])
                          for row in results],
                         [(True, False), (True, True), (False, False), (False, False), (False, False),
                          (False, False), (True, True), (True, False), (False, False)])
        self.assertFalse(any(row["failed"] for row in results))

    def test_invalid_json_identity_types_and_conflicting_modes_fail_closed(self):
        missing = service_status()
        missing.pop("simulation")
        results = self.decisions([
            "not-json", "", "[]", "null", "{}", missing,
            json.dumps([service_status(allow_power_actions=True)]),
            service_status(name="AnotherService", allow_power_actions=True),
            service_status(allow_power_actions="true"),
            service_status(installed="true", allow_power_actions=True),
            service_status(allow_power_actions=True, self_test=True),
            service_status(allow_power_actions=True, simulation=True),
            service_status(allow_power_actions=True, installed=False),
            service_status(allow_ups_poweroff="true"),
            service_status(allow_ups_poweroff=None),
            service_status(allow_ups_poweroff=True),
            service_status(allow_power_actions=True, allow_ups_poweroff=True, self_test=True),
            service_status(allow_power_actions=True, allow_ups_poweroff=True, simulation=True),
            service_status(allow_power_actions=True, allow_ups_poweroff=True, installed=False),
        ])
        self.assertTrue(all(row["failed"] for row in results))
        self.assertTrue(all(row["capabilities"] is None for row in results))

    def test_child_failure_or_unexpected_executor_output_never_grants_permission(self):
        result = self.execute("""
$failed=0
foreach($executor in @(
    {param($program) throw 'Fake access denied'},
    {param($program) [pscustomobject]@{ExitCode=5;StdOut='{}'}},
    {param($program) [pscustomobject]@{ExitCode='0';StdOut='{}'}},
    {param($program) [pscustomobject]@{ExitCode=0;StdOut=@('{}','{}')}},
    {param($program) $null}
)) {
    try {Get-EliproUpgradePowerCapabilities -Executor $executor | Out-Null} catch {$failed++}
}
@{failed=$failed}|ConvertTo-Json -Compress
""")
        self.assertEqual(result["failed"], 5)

    def test_query_uses_fixed_program_files_executable_without_reading_policy(self):
        payload = json.dumps(service_status(allow_power_actions=True))
        result = self.execute("$payload=" + ps_literal(payload) + "\n" + """
$script:seenProgram=$null
$executor={param($program) $script:seenProgram=$program; [pscustomobject]@{ExitCode=0;StdOut=$payload}}
$capabilities=Get-EliproUpgradePowerCapabilities -Executor $executor
$expected=[IO.Path]::Combine([Environment]::GetFolderPath('ProgramFiles'),'Elipro UPS','Elipro.exe')
@{allow=$capabilities.allow_power_actions;ups=$capabilities.allow_ups_poweroff;fixed_path=($script:seenProgram -ceq $expected)}|ConvertTo-Json -Compress
""")
        self.assertTrue(result["allow"])
        self.assertFalse(result["ups"])
        self.assertTrue(result["fixed_path"])

    def test_embedded_reader_works_with_module_autoload_disabled(self):
        payload = json.dumps(service_status(allow_power_actions=True))
        result = self.execute("$payload=" + ps_literal(payload) + "\n" + """
$PSModuleAutoLoadingPreference='None'
$executor={param($program) [pscustomobject]@{ExitCode=0;StdOut=$payload}}
@{capabilities=(Get-EliproUpgradePowerCapabilities -Executor $executor)}|ConvertTo-Json -Compress
""")
        self.assertTrue(result["capabilities"]["allow_power_actions"])
        self.assertFalse(result["capabilities"]["allow_ups_poweroff"])

    def test_encoded_entry_reports_allowed_blocked_and_error_with_distinct_codes(self):
        source = HELPER.read_text(encoding="utf-8")
        entry = source[source.index("if ($MyInvocation.InvocationName -ne '.')"):]
        for fake_body, expected in (
                ("[pscustomobject]@{allow_power_actions=$false;allow_ups_poweroff=$false}", 0),
                ("[pscustomobject]@{allow_power_actions=$true;allow_ups_poweroff=$false}", 1),
                ("[pscustomobject]@{allow_power_actions=$true;allow_ups_poweroff=$true}", 3),
                ("throw 'Fake query failure'", 2)):
            with self.subTest(expected=expected):
                # Substitute the read-only function, while exercising the exact
                # encoded entry block used by Setup. Never invoke the real SCM.
                script = "function Get-EliproUpgradePowerCapabilities {" + fake_body + "}\n" + entry
                encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
                result = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive",
                                         "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                                        capture_output=True, text=True, timeout=20, cwd=ROOT)
                self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
                self.assertEqual(result.stdout.strip(), "")
                if expected == 2:
                    self.assertIn("Cannot determine", result.stderr)

    def test_build_embeds_reader_and_installer_queries_before_stopping_owned_service(self):
        build = (ROOT / "packaging" / "windows" / "Build-Windows.ps1").read_text(encoding="utf-8")
        inno = (ROOT / "packaging" / "windows" / "Elipro.iss").read_text(encoding="utf-8")
        self.assertIn("'Read-ServicePowerCapability.ps1'", build)
        self.assertIn('"/DServicePowerCapability=$capabilityEncoded"', build)
        self.assertIn("'Elipro.exe') ups-power-diagnostic --help", build)
        self.assertLess(build.index("'Elipro.exe') battery-diagnostic --help"),
                        build.index("'Elipro.exe') ups-power-diagnostic --help"))
        query = inno.index("-EncodedCommand {#ServicePowerCapability}")
        self.assertLess(inno.index("-EncodedCommand {#SecurityPreflight}"), query)
        self.assertLess(query, inno.index("'agent --stop'"))
        self.assertIn("PreservePowerActions := False;", inno)
        self.assertIn("PreserveUpsPoweroff := False;", inno)
        self.assertIn("((ResultCode <> 0) and (ResultCode <> 1) and (ResultCode <> 3))", inno)
        guard = inno[inno.index("if PreservePowerActions and"):inno.index("Arguments := Arguments + ' --allow-power-actions'")]
        self.assertIn("{param:SERVICESELFTEST|0}", guard)
        self.assertIn("{param:SERVICESIMULATION|0}", guard)
        ups_guard = inno[inno.index("if PreserveUpsPoweroff and PreservePowerActions and"):
                         inno.index("Arguments := Arguments + ' --allow-ups-poweroff'")]
        self.assertIn("{param:SERVICESELFTEST|0}", ups_guard)
        self.assertIn("{param:SERVICESIMULATION|0}", ups_guard)


if __name__ == "__main__":
    unittest.main()
