"""Signing contracts with fake executors/certificates, never a real private key."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "packaging" / "windows" / "Signing.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(POWERSHELL, "PowerShell is needed to execute the signing helper contracts")
class WindowsSigningTests(unittest.TestCase):
    def execute(self, source, *, file=False):
        test_root = ROOT / ".test-data"
        test_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_root) as directory:
            artifact = Path(directory) / "Elipro sample.exe"
            if file:
                artifact.write_bytes(b"unit-test-placeholder-never-executed")
            script = "$ErrorActionPreference='Stop'\nSet-StrictMode -Version Latest\n"
            script += ". " + ps_literal(HELPER) + "\n"
            script += "$artifact=" + ps_literal(artifact) + "\n"
            script += "$thumb='" + "A" * 40 + "'\n"
            script += source
            result = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive",
                                     "-ExecutionPolicy", "Bypass", "-Command", script],
                                    capture_output=True, text=True, timeout=20, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            return json.loads(result.stdout.strip())

    @staticmethod
    def signing_prelude():
        return """
$signing=[pscustomobject]@{Enabled=$true;Thumbprint=$thumb;SignToolPath='C:\\SDK tools\\signtool.exe';
    TimestampUrl='https://timestamp.example.test';CertificateStoreLocation='CurrentUser'}
$script:commands=[Collections.Generic.List[object]]::new()
$executor={param($tool,$arguments) $script:commands.Add(@{tool=$tool;arguments=@($arguments)}); return 0}
$reader={param($file) [pscustomobject]@{Status='Valid';SignerCertificate=[pscustomobject]@{Thumbprint=$thumb};
    TimeStamperCertificate=[pscustomobject]@{Subject='Fake public timestamp'}}}
"""

    def test_unsigned_default_and_required_signing_fail_before_certificate_access(self):
        result = self.execute("""
function Get-Item { throw 'Certificate lookup must not happen for unsigned defaults' }
$normal=Get-EliproSigningConfiguration
$failed=$false
try { Get-EliproSigningConfiguration -RequireSigning | Out-Null } catch { $failed=$true }
@{unsigned=(-not $normal.Enabled);reason=$normal.Reason;required_failed=$failed}|ConvertTo-Json -Compress
""")
        self.assertTrue(result["unsigned"])
        self.assertIn("unsigned", result["reason"])
        self.assertTrue(result["required_failed"])

    def test_certificate_must_match_thumbprint_private_key_dates_and_code_signing_eku(self):
        result = self.execute("""
function New-FakeCertificate {
    [pscustomobject]@{Thumbprint=$thumb;HasPrivateKey=$true;NotBefore=[datetime]::Now.AddDays(-1);
        NotAfter=[datetime]::Now.AddDays(1);EnhancedKeyUsageList=@([pscustomobject]@{ObjectId='1.3.6.1.5.5.7.3.3'})}
}
$valid=New-FakeCertificate
$accepted=Test-EliproSigningCertificate $valid $thumb
$rejected=0
foreach ($case in @('key','expired','future','eku','thumb')) {
    $bad=New-FakeCertificate
    switch ($case) {
        'key' {$bad.HasPrivateKey=$false}
        'expired' {$bad.NotAfter=[datetime]::Now.AddSeconds(-1)}
        'future' {$bad.NotBefore=[datetime]::Now.AddDays(1)}
        'eku' {$bad.EnhancedKeyUsageList=@([pscustomobject]@{ObjectId='1.3.6.1.5.5.7.3.1'})}
        'thumb' {$bad.Thumbprint='B'*40}
    }
    try { Test-EliproSigningCertificate $bad $thumb | Out-Null } catch { $rejected++ }
}
$valid.EnhancedKeyUsageList=@([System.Security.Cryptography.Oid]::new('1.3.6.1.5.5.7.3.3'))
$oidAccepted=Test-EliproSigningCertificate $valid $thumb
@{accepted=$accepted;rejected=$rejected;oid_accepted=$oidAccepted}|ConvertTo-Json -Compress
""")
        self.assertEqual(result["accepted"], "A" * 40)
        self.assertEqual(result["oid_accepted"], "A" * 40)
        self.assertEqual(result["rejected"], 5)

    def test_sign_then_verify_uses_exact_thumbprint_sha256_rfc3161_and_authenticode_policy(self):
        result = self.execute(self.signing_prelude() + """
$signed=Invoke-EliproSignFile $artifact $signing -Executor $executor -SignatureReader $reader
@{commands=@($script:commands.ToArray());result=$signed}|ConvertTo-Json -Depth 7 -Compress
""", file=True)
        sign, verify = [item["arguments"] for item in result["commands"]]
        self.assertEqual(sign[:6], ["sign", "/sha1", "A" * 40, "/s", "My", "/fd"])
        self.assertEqual(sign[sign.index("/fd") + 1], "SHA256")
        self.assertEqual(sign[sign.index("/td") + 1], "SHA256")
        self.assertEqual(sign[sign.index("/tr") + 1], "https://timestamp.example.test")
        self.assertNotIn("/f", sign)
        self.assertNotIn("/p", sign)
        self.assertEqual(verify[:4], ["verify", "/pa", "/all", "/tw"])
        self.assertEqual(sign[-1], verify[-1])
        self.assertTrue(result["result"]["verified"])
        self.assertTrue(result["result"]["timestamped"])
        self.assertEqual(result["result"]["file"], "Elipro sample.exe")

    def test_machine_certificate_store_is_explicit_not_implicit(self):
        result = self.execute(self.signing_prelude() + """
$signing.CertificateStoreLocation='LocalMachine'
$null=Invoke-EliproSignFile $artifact $signing -Executor $executor -SignatureReader $reader
@{arguments=$script:commands[0].arguments}|ConvertTo-Json -Depth 4 -Compress
""", file=True)
        self.assertIn("/sm", result["arguments"])

    def test_signtool_errors_and_timestamp_warnings_are_fatal(self):
        result = self.execute(self.signing_prelude() + """
$failed=0
foreach ($code in @(1,2)) {
    $failing={param($tool,$arguments) return $code}.GetNewClosure()
    try { Invoke-EliproSignFile $artifact $signing -Executor $failing -SignatureReader $reader | Out-Null }
    catch { $failed++ }
}
@{failed=$failed}|ConvertTo-Json -Compress
""", file=True)
        self.assertEqual(result["failed"], 2)

    def test_verification_rejects_other_signer_absent_timestamp_and_untrusted_signature(self):
        result = self.execute(self.signing_prelude() + """
$failed=0
foreach ($case in @('thumb','timestamp','untrusted')) {
    $badReader={param($file)
        [pscustomobject]@{Status=$(if ($case -eq 'untrusted') {'NotTrusted'} else {'Valid'});
            SignerCertificate=[pscustomobject]@{Thumbprint=$(if($case -eq 'thumb') {'B'*40} else {$thumb})};
            TimeStamperCertificate=$(if($case -eq 'timestamp') {$null} else {[pscustomobject]@{Subject='Fake'}})}
    }.GetNewClosure()
    try { Test-EliproArtifactSignature $artifact $signing -Executor $executor -SignatureReader $badReader | Out-Null }
    catch { $failed++ }
}
@{failed=$failed}|ConvertTo-Json -Compress
""", file=True)
        self.assertEqual(result["failed"], 3)

    def test_invalid_timestamp_and_malformed_thumbprint_fail_before_store_access(self):
        result = self.execute("""
function Get-Item { throw 'Unexpected certificate store access' }
$failed=0
foreach($url in @('http://timestamp.example.test','file:///tmp/test','https://user:secret@example.test')) {
    try { Get-EliproSigningConfiguration -SigningCertificateThumbprint $thumb -TimestampUrl $url | Out-Null }
    catch { if($_.Exception.Message -like '*HTTPS*') {$failed++} }
}
try { ConvertTo-EliproCertificateThumbprint '../other' | Out-Null } catch { $failed++ }
@{failed=$failed}|ConvertTo-Json -Compress
""")
        self.assertEqual(result["failed"], 4)

    def test_allowlist_signs_owned_executables_without_resigning_vendor_runtime(self):
        result = self.execute("""
$app=Split-Path -Parent $artifact
[IO.File]::WriteAllText((Join-Path $app 'Elipro.exe'),'fake')
[IO.File]::WriteAllText((Join-Path $app 'EliproLauncher.exe'),'fake')
$vendor=Join-Path $app '_internal'
New-Item -ItemType Directory -Path $vendor | Out-Null
[IO.File]::WriteAllText((Join-Path $vendor 'python314.dll'),'vendor')
$files=@(Get-EliproOwnedBinaries $app | ForEach-Object {[IO.Path]::GetFileName($_)})
@{files=$files}|ConvertTo-Json -Compress
""")
        self.assertEqual(result["files"], ["Elipro.exe", "EliproLauncher.exe"])

    def test_inno_signing_quotes_paths_and_uses_the_verified_helper(self):
        result = self.execute(self.signing_prelude() + """
$options=Get-EliproInnoSigningArguments $signing 'C:\\repo with spaces\\Sign-Artifact.ps1' 'C:\\Windows\\powershell.exe'
$failed=$false
try { Get-EliproInnoSigningArguments $signing 'C:\\repo$bad\\Sign-Artifact.ps1' 'C:\\Windows\\powershell.exe' | Out-Null }
catch { $failed=$true }
@{options=@($options);rejected_expansion=$failed}|ConvertTo-Json -Compress
""")
        self.assertEqual(result["options"][0], "/DEnableSigning=1")
        self.assertIn("/SEliproSigning=", result["options"][1])
        self.assertIn("-FilePath $f", result["options"][1])
        self.assertIn("$qC:\\repo with spaces\\Sign-Artifact.ps1$q", result["options"][1])
        self.assertTrue(result["rejected_expansion"])

    def test_build_scripts_parse_and_final_hash_follows_signature_verification(self):
        paths = [ROOT / "packaging" / "windows" / name for name in
                 ("Build-Windows.ps1", "Sign-Artifact.ps1", "Signing.ps1")]
        result = self.execute("$paths=@(" + ",".join(ps_literal(path) for path in paths) + ")\n" + """
$errors=@()
foreach($path in $paths) {
    $tokens=$null;$parseErrors=$null
    [void][Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$parseErrors)
    $errors+=@($parseErrors)
}
@{errors=$errors.Count}|ConvertTo-Json -Compress
""")
        self.assertEqual(result["errors"], 0)
        build = paths[0].read_text(encoding="utf-8")
        self.assertGreater(build.index("$digest = (Get-FileHash"), build.index("Test-EliproArtifactSignature -FilePath $installerPath"))
        self.assertIn("signing-status.json", build)
        inno = (ROOT / "packaging" / "windows" / "Elipro.iss").read_text(encoding="utf-8")
        self.assertIn("#ifdef EnableSigning\nSignTool=EliproSigning\nSignedUninstaller=yes", inno)
        spec = (ROOT / "packaging" / "elipro.spec").read_text(encoding="utf-8")
        self.assertIn('contents_directory="_internal"', spec)
        self.assertIn("exclude_binaries=True", spec)
        self.assertIn("COLLECT(exe", spec)

    def test_signed_ci_guards_reject_before_reading_certificates_or_importing_credentials(self):
        signed_ci = ROOT / "packaging" / "windows" / "Build-SignedCI.ps1"
        source = "$signedCi=" + ps_literal(signed_ci) + "\n" + """
$global:eliproTestStoreCalls=0;$global:eliproTestImportCalls=0
function Get-ChildItem {$global:eliproTestStoreCalls++;throw 'Unexpected certificate store access'}
function Get-PfxData {$global:eliproTestImportCalls++;throw 'Unexpected PFX access'}
function Import-PfxCertificate {$global:eliproTestImportCalls++;throw 'Unexpected PFX import'}
$cases=@(
    @{name='local';actions='false';event='workflow_dispatch';ref='refs/heads/main';os='Windows';arch='X64';secret='fake'},
    @{name='pull_request';actions='true';event='pull_request';ref='refs/pull/1/merge';os='Windows';arch='X64';secret='fake'},
    @{name='manual_feature';actions='true';event='workflow_dispatch';ref='refs/heads/feature';os='Windows';arch='X64';secret='fake'},
    @{name='manual_main_missing';actions='true';event='workflow_dispatch';ref='refs/heads/main';os='Windows';arch='X64';secret=''},
    @{name='manual_tag_missing';actions='true';event='workflow_dispatch';ref='refs/tags/v1.2.3';os='Windows';arch='X64';secret=''},
    @{name='wrong_runner';actions='true';event='workflow_dispatch';ref='refs/heads/main';os='Linux';arch='X64';secret='fake'}
)
$results=@()
foreach($case in $cases) {
    $env:GITHUB_ACTIONS=$case.actions;$env:GITHUB_EVENT_NAME=$case.event;$env:GITHUB_REF=$case.ref
    $env:RUNNER_OS=$case.os;$env:RUNNER_ARCH=$case.arch
    $env:ELIPRO_SIGNING_PFX_BASE64=$case.secret;$env:ELIPRO_SIGNING_PFX_PASSWORD=$case.secret
    $message=$null
    try { & $signedCi | Out-Null } catch {$message=$_.Exception.Message}
    $results+=@{name=$case.name;message=$message}
}
@{results=$results;store_calls=$global:eliproTestStoreCalls;import_calls=$global:eliproTestImportCalls}|ConvertTo-Json -Depth 5 -Compress
"""
        result = self.execute(source)
        self.assertEqual(result["store_calls"], 0)
        self.assertEqual(result["import_calls"], 0)
        results = {item["name"]: item["message"] for item in result["results"]}
        for name in ("local", "pull_request", "manual_feature"):
            self.assertIn("manual run from main or a version tag", results[name])
        for name in ("manual_main_missing", "manual_tag_missing"):
            self.assertIn("certificate secrets", results[name])
        self.assertIn("disposable Windows x64 runner", results["wrong_runner"])

    def test_signed_ci_invalid_base64_hides_input_clears_environment_and_mutates_no_store_or_file(self):
        signed_ci = ROOT / "packaging" / "windows" / "Build-SignedCI.ps1"
        source = "$signedCi=" + ps_literal(signed_ci) + "\n" + """
$global:eliproTestStoreCalls=0;$global:eliproTestImportCalls=0
function Get-ChildItem {
    param($LiteralPath)
    if ($LiteralPath -ne 'Cert:\\CurrentUser\\My') {throw 'Unexpected store lookup'}
    $global:eliproTestStoreCalls++
    return @()
}
function Get-PfxData {$global:eliproTestImportCalls++;throw 'Unexpected PFX read'}
function Import-PfxCertificate {$global:eliproTestImportCalls++;throw 'Unexpected PFX import'}
$env:GITHUB_ACTIONS='true';$env:GITHUB_EVENT_NAME='workflow_dispatch';$env:GITHUB_REF='refs/heads/main'
$env:RUNNER_OS='Windows';$env:RUNNER_ARCH='X64';$env:RUNNER_TEMP=Split-Path -Parent $artifact
$env:ELIPRO_SIGNING_PFX_BASE64='!!! not base64 !!!';$env:ELIPRO_SIGNING_PFX_PASSWORD='unit-test-placeholder'
$message=$null
try { & $signedCi | Out-Null } catch {$message=$_.Exception.Message}
@{message=$message;store_calls=$global:eliproTestStoreCalls;import_calls=$global:eliproTestImportCalls;
  base64_remaining=(Test-Path -LiteralPath Env:\\ELIPRO_SIGNING_PFX_BASE64);
  password_remaining=(Test-Path -LiteralPath Env:\\ELIPRO_SIGNING_PFX_PASSWORD);
  files_remaining=[IO.Directory]::GetFiles($env:RUNNER_TEMP).Count}|ConvertTo-Json -Compress
"""
        result = self.execute(source)
        self.assertEqual(result["message"], "The signing certificate could not be imported. Certificate contents and password are not logged.")
        self.assertEqual(result["import_calls"], 0)
        self.assertEqual(result["store_calls"], 2)  # snapshot and finally, both read-only mocks
        self.assertFalse(result["base64_remaining"])
        self.assertFalse(result["password_remaining"])
        self.assertEqual(result["files_remaining"], 0)


if __name__ == "__main__":
    unittest.main()
