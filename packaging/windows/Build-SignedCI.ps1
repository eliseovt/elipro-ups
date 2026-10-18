[CmdletBinding()]
param([string]$Python = 'python')

$ErrorActionPreference = 'Stop'
# Signing credentials belong to the protected release-signing environment.
# Pull requests and ordinary builds must never execute this credential path.
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:GITHUB_EVENT_NAME -ne 'workflow_dispatch' -or
    ($env:GITHUB_REF -ne 'refs/heads/main' -and $env:GITHUB_REF -notlike 'refs/tags/v*')) {
    throw 'Signing in CI requires a manual run from main or a version tag.'
}
if ($env:RUNNER_OS -ne 'Windows' -or $env:RUNNER_ARCH -ne 'X64') {
    throw 'Signing requires the disposable Windows x64 runner.'
}
if (-not $env:ELIPRO_SIGNING_PFX_BASE64 -or -not $env:ELIPRO_SIGNING_PFX_PASSWORD) {
    throw 'Configure the code-signing certificate secrets in the release-signing environment.'
}

$certificateFile = Join-Path $env:RUNNER_TEMP ('elipro-signing-' + [Guid]::NewGuid().ToString('N') + '.pfx')
$previousThumbprints = @(Get-ChildItem -LiteralPath 'Cert:\CurrentUser\My' | ForEach-Object Thumbprint)
$importedCertificates = @()
$pfxThumbprints = @()
$certificateBytes = $null
$password = $null
try {
    try {
        $certificateBytes = [Convert]::FromBase64String($env:ELIPRO_SIGNING_PFX_BASE64)
        [IO.File]::WriteAllBytes($certificateFile, $certificateBytes)
        [Array]::Clear($certificateBytes, 0, $certificateBytes.Length)
        $password = ConvertTo-SecureString -String $env:ELIPRO_SIGNING_PFX_PASSWORD -AsPlainText -Force
        $pfxData = Get-PfxData -FilePath $certificateFile -Password $password
        $pfxThumbprints = @(@($pfxData.EndEntityCertificates) + @($pfxData.OtherCertificates) | ForEach-Object Thumbprint)
        if (@($pfxThumbprints | Where-Object { $previousThumbprints -contains $_ }).Count -gt 0) {
            throw 'The disposable runner already contained a certificate from this PFX.'
        }
        $importedCertificates = @(Import-PfxCertificate -FilePath $certificateFile -Password $password -CertStoreLocation 'Cert:\CurrentUser\My')
    }
    catch {
        throw 'The signing certificate could not be imported. Certificate contents and password are not logged.'
    }
    finally {
        if ($password) { $password.Dispose() }
        if ($certificateBytes) { [Array]::Clear($certificateBytes, 0, $certificateBytes.Length) }
        Remove-Item -LiteralPath 'Env:\ELIPRO_SIGNING_PFX_BASE64', 'Env:\ELIPRO_SIGNING_PFX_PASSWORD' -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $certificateFile) { Remove-Item -LiteralPath $certificateFile -Force }
    }

    $codeSigningThumbprints = @(Get-ChildItem -LiteralPath 'Cert:\CurrentUser\My' -CodeSigningCert | ForEach-Object Thumbprint)
    $candidates = @($importedCertificates | Where-Object {
        $_.HasPrivateKey -and $_.NotBefore -le (Get-Date) -and $_.NotAfter -gt (Get-Date) -and
        $codeSigningThumbprints -contains $_.Thumbprint
    })
    if ($candidates.Count -ne 1) { throw 'Exactly one current code-signing certificate with a private key is required.' }
    if ($previousThumbprints -contains $candidates[0].Thumbprint) {
        throw 'The disposable runner already contained this signing certificate.'
    }
    $buildOptions = @{ Python = $Python; SigningCertificateThumbprint = $candidates[0].Thumbprint; RequireSigning = $true }
    if ($env:ELIPRO_SIGNING_TIMESTAMP_URL) { $buildOptions.TimestampUrl = $env:ELIPRO_SIGNING_TIMESTAMP_URL }
    & (Join-Path $PSScriptRoot 'Build-Windows.ps1') @buildOptions
}
finally {
    # Remove only newly imported certificates. Never modify a trust store or
    # install a self-signed root to make verification appear successful.
    $newCertificates = @(Get-ChildItem -LiteralPath 'Cert:\CurrentUser\My' | Where-Object {
        $previousThumbprints -notcontains $_.Thumbprint -and $pfxThumbprints -contains $_.Thumbprint
    })
    foreach ($certificate in $newCertificates) {
        $certificatePath = 'Cert:\CurrentUser\My\' + $certificate.Thumbprint
        if ($certificate.HasPrivateKey) { Remove-Item -LiteralPath $certificatePath -DeleteKey -Force }
        else { Remove-Item -LiteralPath $certificatePath -Force }
    }
    if (Test-Path -LiteralPath $certificateFile) { Remove-Item -LiteralPath $certificateFile -Force }
}
