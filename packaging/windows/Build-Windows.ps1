[CmdletBinding()]
param(
    [string]$Python = 'python',
    [string]$InnoCompiler = '',
    [string]$SigningCertificateThumbprint = '',
    [string]$SignToolPath = '',
    [string]$TimestampUrl = 'https://timestamp.digicert.com',
    [ValidateSet('CurrentUser', 'LocalMachine')][string]$CertificateStoreLocation = 'CurrentUser',
    [switch]$RequireSigning
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$appOutput = Join-Path $repoRoot 'dist\Elipro'
$installerOutput = Join-Path $repoRoot 'dist\installer'
. (Join-Path $PSScriptRoot 'Signing.ps1')

if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem) {
    throw 'The installer must be built on 64-bit Windows.'
}

if (-not $InnoCompiler) {
    $InnoCompiler = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'
}
if (-not (Test-Path -LiteralPath $InnoCompiler -PathType Leaf)) {
    throw 'Install Inno Setup 6, or pass its ISCC.exe path with -InnoCompiler.'
}
$signing = Get-EliproSigningConfiguration -SigningCertificateThumbprint $SigningCertificateThumbprint `
    -SignToolPath $SignToolPath -TimestampUrl $TimestampUrl -CertificateStoreLocation $CertificateStoreLocation `
    -RequireSigning:$RequireSigning
$verifiedSignatures = [Collections.Generic.List[object]]::new()
if (-not $signing.Enabled) { Write-Warning $signing.Reason }

Push-Location -LiteralPath $repoRoot
try {
    & $Python -c 'import struct, sys; assert sys.platform == "win32" and struct.calcsize("P") == 8, "Python x64 on Windows is required"'
    if ($LASTEXITCODE -ne 0) { throw 'Python must be a 64-bit Windows runtime.' }

    $appVersion = (& $Python -c 'from ups_monitor import __version__; print(__version__)').Trim()
    if ($LASTEXITCODE -ne 0 -or $appVersion -notmatch '^\d+\.\d+\.\d+$') {
        throw 'ups_monitor.__version__ must have a numeric major.minor.patch version.'
    }
    Write-Host "Building Elipro $appVersion for Windows x64"
    & $Python -m PyInstaller --noconfirm --clean packaging/elipro.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

    $csharpCompiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    if (-not (Test-Path -LiteralPath $csharpCompiler -PathType Leaf)) {
        throw 'The .NET Framework x64 C# compiler was not found.'
    }
    $launcherPath = Join-Path $appOutput 'EliproLauncher.exe'
    $iconPath = Join-Path $repoRoot 'static\assets\elipro.ico'
    if (-not (Test-Path -LiteralPath $iconPath -PathType Leaf)) { throw 'Elipro application icon is missing.' }
    & $csharpCompiler /nologo /target:winexe /platform:x64 /optimize+ /reference:System.Windows.Forms.dll "/win32icon:$iconPath" "/out:$launcherPath" (Join-Path $PSScriptRoot 'EliproLauncher.cs')
    if ($LASTEXITCODE -ne 0) { throw 'The GUI launcher could not be compiled.' }

    # Sign project-owned executables only. Vendor DLLs/PYD/runtime files under
    # _internal retain their original signatures and are never re-signed.
    $ownedBinaries = @(Get-EliproOwnedBinaries -AppOutput $appOutput)
    if ($signing.Enabled) {
        foreach ($binary in $ownedBinaries) {
            $verifiedSignatures.Add((Invoke-EliproSignFile -FilePath $binary -Signing $signing))
        }
    }

    # Preserve required notices for the Python runtime and PyInstaller bootloader.
    $licenseMetadata = @'
import importlib.metadata
import json
import sys
from pathlib import Path
python_license = Path(sys.base_prefix) / "LICENSE.txt"
distribution = importlib.metadata.distribution("pyinstaller")
licenses = [distribution.locate_file(item) for item in distribution.files or []
            if item.name.lower() == "copying.txt"]
assert python_license.is_file(), "Python LICENSE.txt not found"
assert licenses and licenses[0].is_file(), "PyInstaller COPYING.txt not found"
print(json.dumps({"python": str(python_license), "pyinstaller": str(licenses[0])}))
'@
    $licenseJson = & $Python -c $licenseMetadata
    if ($LASTEXITCODE -ne 0) { throw 'Runtime license collection failed.' }
    $licenses = $licenseJson | ConvertFrom-Json
    $licenseOutput = Join-Path $appOutput 'licenses'
    New-Item -ItemType Directory -Force -Path $licenseOutput | Out-Null
    Copy-Item -LiteralPath $licenses.python -Destination (Join-Path $licenseOutput 'Python-LICENSE.txt')
    Copy-Item -LiteralPath $licenses.pyinstaller -Destination (Join-Path $licenseOutput 'PyInstaller-COPYING.txt')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'THIRD-PARTY-NOTICES.txt') -Destination $licenseOutput

    & (Join-Path $appOutput 'Elipro.exe') smoke-test
    if ($LASTEXITCODE -ne 0) { throw 'The bundled executable failed its non-invasive smoke test.' }
    & (Join-Path $appOutput 'Elipro.exe') battery-diagnostic --help
    if ($LASTEXITCODE -ne 0) { throw 'The bundled battery diagnostic failed its non-invasive help check.' }

    New-Item -ItemType Directory -Force -Path $installerOutput | Out-Null
    $preflightText = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'Protect-InstallPaths.ps1'))
    $preflightEncoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($preflightText))
    $innoArguments = @("/DAppVersion=$appVersion", "/DSourceDir=$appOutput", "/DOutputDir=$installerOutput",
                       "/DSecurityPreflight=$preflightEncoded")
    if ($signing.Enabled) {
        $powershellPath = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $innoArguments += Get-EliproInnoSigningArguments -Signing $signing `
            -HelperPath (Join-Path $PSScriptRoot 'Sign-Artifact.ps1') -PowerShellPath $powershellPath
    }
    $innoArguments += Join-Path $PSScriptRoot 'Elipro.iss'
    # Inno invokes Sign-Artifact for both its final Setup and its embedded
    # uninstaller. Each invocation signs and verifies, with warnings fatal.
    & $InnoCompiler @innoArguments
    if ($LASTEXITCODE -ne 0) { throw 'Inno Setup compilation failed.' }

    $installerPath = Join-Path $installerOutput 'Elipro-Setup-x64.exe'
    if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) { throw 'Installer output is missing.' }
    if ($signing.Enabled) {
        $verifiedSignatures.Add((Test-EliproArtifactSignature -FilePath $installerPath -Signing $signing))
    }
    # Hash only the final signed bytes. Signing changes a PE file's digest.
    $digest = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText("$installerPath.sha256", "$digest  Elipro-Setup-x64.exe`n", [Text.Encoding]::ASCII)
    $signingReport = [ordered]@{
        schema_version = 1; application = 'Elipro UPS'; version = $appVersion
        signed = [bool]$signing.Enabled; certificate_thumbprint = $signing.Thumbprint
        verified_files = @($verifiedSignatures.ToArray()); signed_uninstaller = [bool]$signing.Enabled
        digest_algorithm = 'SHA256'; timestamp_protocol = $(if ($signing.Enabled) { 'RFC3161-SHA256' } else { $null })
        installer_sha256 = $digest; reason = $signing.Reason
    }
    $reportPath = Join-Path $installerOutput 'signing-status.json'
    [IO.File]::WriteAllText($reportPath, ($signingReport | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
    Write-Host "Ready: $installerPath"
    Write-Host "SHA256: $digest"
}
finally {
    Pop-Location
}
