# Pure helper definitions; dot-sourcing never imports a certificate or changes trust.
function ConvertTo-EliproCertificateThumbprint {
    param([Parameter(Mandatory)][string]$Thumbprint)
    $normalized = ($Thumbprint -replace '\s', '').ToUpperInvariant()
    if ($normalized -notmatch '^[A-F0-9]{40}$') {
        throw 'The signing certificate thumbprint must contain exactly 40 hexadecimal characters.'
    }
    return $normalized
}

function Test-EliproSigningCertificate {
    param([Parameter(Mandatory)]$Certificate, [Parameter(Mandatory)][string]$ExpectedThumbprint,
          [datetime]$Now = [datetime]::Now)
    $expected = ConvertTo-EliproCertificateThumbprint $ExpectedThumbprint
    if ((ConvertTo-EliproCertificateThumbprint $Certificate.Thumbprint) -ne $expected) {
        throw 'The certificate store returned an unexpected signing certificate.'
    }
    if (-not $Certificate.HasPrivateKey) { throw 'The signing certificate has no private key.' }
    if ($Certificate.NotBefore -gt $Now -or $Certificate.NotAfter -le $Now) {
        throw 'The signing certificate is not currently valid.'
    }
    $codeSigning = @($Certificate.EnhancedKeyUsageList | Where-Object {
        $oid = $null
        if ($_ -is [System.Security.Cryptography.Oid]) { $oid = $_.Value }
        elseif ($_.PSObject.Properties['ObjectId']) {
            $objectId = $_.ObjectId
            if ($objectId -is [string]) { $oid = $objectId }
            elseif ($null -ne $objectId -and $objectId.PSObject.Properties['Value']) { $oid = $objectId.Value }
        }
        $oid -eq '1.3.6.1.5.5.7.3.3'
    })
    if ($codeSigning.Count -eq 0) { throw 'The certificate must explicitly allow the Code Signing EKU.' }
    return $expected
}

function Resolve-EliproSignTool {
    param([string]$SignToolPath = '')
    if (-not $SignToolPath) {
        $found = Get-Command signtool.exe -ErrorAction SilentlyContinue
        if ($found) { $SignToolPath = $found.Source }
    }
    if (-not $SignToolPath) {
        $sdkBin = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
        if (Test-Path -LiteralPath $sdkBin -PathType Container) {
            $candidates = @(Get-ChildItem -LiteralPath $sdkBin -Directory | Where-Object {
                $_.Name -match '^\d+\.\d+\.\d+\.\d+$'
            } | Sort-Object { [version]$_.Name } -Descending | ForEach-Object {
                Join-Path $_.FullName 'x64\signtool.exe'
            } | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
            if ($candidates.Count) { $SignToolPath = $candidates[0] }
        }
    }
    if (-not $SignToolPath -or -not (Test-Path -LiteralPath $SignToolPath -PathType Leaf)) {
        throw 'A Windows SDK signtool.exe is required when signing is requested; pass -SignToolPath.'
    }
    return [IO.Path]::GetFullPath($SignToolPath)
}

function Get-EliproSigningConfiguration {
    param([string]$SigningCertificateThumbprint = '', [string]$SignToolPath = '',
          [string]$TimestampUrl = 'https://timestamp.digicert.com',
          [ValidateSet('CurrentUser', 'LocalMachine')][string]$CertificateStoreLocation = 'CurrentUser',
          [switch]$RequireSigning)
    if (-not $SigningCertificateThumbprint) {
        if ($RequireSigning) { throw 'RequireSigning needs an explicit -SigningCertificateThumbprint.' }
        return [pscustomobject]@{ Enabled = $false; State = 'unsigned'; Thumbprint = $null;
            Reason = 'No signing certificate was configured. This build is unsigned.' }
    }
    $thumbprint = ConvertTo-EliproCertificateThumbprint $SigningCertificateThumbprint
    $timestamp = $null
    if (-not [Uri]::TryCreate($TimestampUrl, [UriKind]::Absolute, [ref]$timestamp) -or
        $timestamp.Scheme -ne 'https' -or $timestamp.UserInfo -or $timestamp.Fragment) {
        throw 'TimestampUrl must be an absolute HTTPS RFC3161 endpoint without credentials or fragments.'
    }
    $certificate = Get-Item -LiteralPath "Cert:\$CertificateStoreLocation\My\$thumbprint" -ErrorAction Stop
    $null = Test-EliproSigningCertificate $certificate $thumbprint
    return [pscustomobject]@{ Enabled = $true; State = 'configured'; Thumbprint = $thumbprint;
        SignToolPath = (Resolve-EliproSignTool $SignToolPath); TimestampUrl = $timestamp.AbsoluteUri;
        CertificateStoreLocation = $CertificateStoreLocation; Reason = $null }
}

function Invoke-EliproSignTool {
    param([Parameter(Mandatory)][string]$ToolPath, [Parameter(Mandatory)][string[]]$Arguments,
          [scriptblock]$Executor)
    if ($null -ne $Executor) {
        $exitCode = & $Executor $ToolPath $Arguments
    }
    else {
        & $ToolPath @Arguments | Out-Host
        $exitCode = $LASTEXITCODE
    }
    # SignTool uses 2 for warnings, including timestamp failures. A signed
    # release must fail closed on warnings as well as errors.
    if ($exitCode -ne 0) { throw "SignTool failed or reported a warning (exit $exitCode)." }
}

function Test-EliproArtifactSignature {
    param([Parameter(Mandatory)][string]$FilePath, [Parameter(Mandatory)]$Signing,
          [scriptblock]$Executor, [scriptblock]$SignatureReader)
    Invoke-EliproSignTool $Signing.SignToolPath @('verify', '/pa', '/all', '/tw', $FilePath) -Executor $Executor
    $signature = if ($null -ne $SignatureReader) { & $SignatureReader $FilePath }
                 else { Get-AuthenticodeSignature -LiteralPath $FilePath }
    if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate) {
        throw 'Authenticode verification did not confirm a trusted valid signature.'
    }
    if ((ConvertTo-EliproCertificateThumbprint $signature.SignerCertificate.Thumbprint) -ne $Signing.Thumbprint) {
        throw 'The signed artifact does not match the explicitly selected certificate thumbprint.'
    }
    if ($null -eq $signature.TimeStamperCertificate) {
        throw 'The signed artifact has no verified timestamp certificate.'
    }
    return [pscustomobject]@{ file = [IO.Path]::GetFileName($FilePath); verified = $true;
                             timestamped = $true; certificate_thumbprint = $Signing.Thumbprint }
}

function Invoke-EliproSignFile {
    param([Parameter(Mandatory)][string]$FilePath, [Parameter(Mandatory)]$Signing,
          [scriptblock]$Executor, [scriptblock]$SignatureReader)
    if (-not $Signing.Enabled) { throw 'Artifact signing requires an explicitly configured certificate.' }
    if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) { throw 'The artifact to sign does not exist.' }
    $arguments = @('sign', '/sha1', $Signing.Thumbprint, '/s', 'My', '/fd', 'SHA256',
                   '/tr', $Signing.TimestampUrl, '/td', 'SHA256', '/d', 'Elipro UPS')
    if ($Signing.CertificateStoreLocation -eq 'LocalMachine') { $arguments += '/sm' }
    $arguments += $FilePath
    Invoke-EliproSignTool $Signing.SignToolPath $arguments -Executor $Executor
    return Test-EliproArtifactSignature $FilePath $Signing -Executor $Executor -SignatureReader $SignatureReader
}

function Get-EliproOwnedBinaries {
    param([Parameter(Mandatory)][string]$AppOutput)
    # Never recurse into _internal: Python, OpenSSL and other vendors keep their
    # original signatures. These are the two executables built by this project.
    foreach ($name in @('Elipro.exe', 'EliproLauncher.exe')) {
        $file = Join-Path $AppOutput $name
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required Elipro binary is missing: $name" }
        $file
    }
}

function Get-EliproInnoSigningArguments {
    param([Parameter(Mandatory)]$Signing, [Parameter(Mandatory)][string]$HelperPath,
          [Parameter(Mandatory)][string]$PowerShellPath)
    foreach ($value in @($HelperPath, $PowerShellPath, $Signing.SignToolPath, $Signing.TimestampUrl)) {
        # Inno expands $q/$f itself. None of our fixed argument values may
        # introduce expansion tokens or quote boundaries into that command.
        if ($value -match '["$\r\n]') { throw 'A signing tool argument contains unsupported Inno command characters.' }
    }
    $command = ('$q{0}$q -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $q{1}$q ' +
                '-FilePath $f -SigningCertificateThumbprint {2} -SignToolPath $q{3}$q ' +
                '-TimestampUrl $q{4}$q -CertificateStoreLocation {5}') -f
                $PowerShellPath, $HelperPath, $Signing.Thumbprint, $Signing.SignToolPath,
                $Signing.TimestampUrl, $Signing.CertificateStoreLocation
    return @('/DEnableSigning=1', "/SEliproSigning=$command")
}
