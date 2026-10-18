[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$FilePath,
    [Parameter(Mandatory)][string]$SigningCertificateThumbprint,
    [Parameter(Mandatory)][string]$SignToolPath,
    [Parameter(Mandatory)][string]$TimestampUrl,
    [ValidateSet('CurrentUser', 'LocalMachine')][string]$CertificateStoreLocation = 'CurrentUser'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Signing.ps1')
$signing = Get-EliproSigningConfiguration -SigningCertificateThumbprint $SigningCertificateThumbprint `
    -SignToolPath $SignToolPath -TimestampUrl $TimestampUrl -CertificateStoreLocation $CertificateStoreLocation -RequireSigning
$null = Invoke-EliproSignFile -FilePath $FilePath -Signing $signing
