[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true') {
    throw 'This installation test is restricted to the disposable GitHub Actions runner.'
}
if ($env:RUNNER_OS -ne 'Windows' -or $env:RUNNER_ARCH -ne 'X64') {
    throw 'A Windows x64 hosted runner is required.'
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$installer = Join-Path $repoRoot 'dist\installer\Elipro-Setup-x64.exe'
$logs = Join-Path $repoRoot 'dist\installer-test'
$installRoot = Join-Path $env:ProgramFiles 'Elipro UPS'
$installedExe = Join-Path $installRoot 'Elipro.exe'
$launcherExe = Join-Path $installRoot 'EliproLauncher.exe'
$uninstaller = Join-Path $installRoot 'unins000.exe'
$serviceName = 'EliproUPS'
New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Assert-InstallerRejects([string]$Label, [string[]]$ExtraArguments) {
    $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SERVICESELFTEST=1',
        ('/LOG="' + (Join-Path $logs "$Label.log") + '"')) + $ExtraArguments
    $attempt = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
    if ($attempt.ExitCode -eq 0) { throw "Installer accepted unsafe case: $Label." }
    if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) { throw "Unsafe case registered a service: $Label." }
}

# These rejection cases run only after the disposable-runner guards above.
$unsafeDestination = Join-Path $logs 'custom-destination'
Assert-InstallerRejects 'reject-custom-directory' @(('/DIR="' + $unsafeDestination + '"'))
if (Test-Path -LiteralPath $unsafeDestination) { throw 'The custom destination was written.' }

$machineRoot = Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'Elipro UPS'
if (Test-Path -LiteralPath $machineRoot) { throw 'The disposable runner already contains an Elipro data directory.' }
$junctionTarget = Join-Path $logs 'junction-target'
New-Item -ItemType Directory -Path $junctionTarget | Out-Null
$untouchedMarker = Join-Path $junctionTarget 'untouched.txt'
[IO.File]::WriteAllText($untouchedMarker, 'Must survive the rejected installer')
New-Item -ItemType Junction -Path $machineRoot -Target $junctionTarget | Out-Null
try {
    Assert-InstallerRejects 'reject-data-junction' @()
    if ([IO.File]::ReadAllText($untouchedMarker) -ne 'Must survive the rejected installer') { throw 'The junction target was modified.' }
}
finally {
    # Delete only this verified junction itself, never its target or contents.
    $fixture = Get-Item -LiteralPath $machineRoot -Force
    if ($fixture.FullName -ne $machineRoot -or -not ($fixture.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Refusing to remove an unexpected junction fixture.'
    }
    [IO.Directory]::Delete($machineRoot)
}

New-Item -ItemType Directory -Path $machineRoot | Out-Null
try {
    Assert-InstallerRejects 'reject-inherited-data-acl' @()
}
finally {
    # The rejected preflight must leave this empty, non-reparse fixture intact.
    $fixture = Get-Item -LiteralPath $machineRoot -Force
    if ($fixture.FullName -ne $machineRoot -or ($fixture.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        @(Get-ChildItem -LiteralPath $machineRoot -Force).Count -ne 0) {
        throw 'Refusing to remove an unexpected directory fixture.'
    }
    [IO.Directory]::Delete($machineRoot)
}

# The self-test service enters SCM normally, but never starts the monitor,
# queries USB, opens the panel, or requests any Windows power action.
$installArgs = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SERVICESELFTEST=1',
    ('/LOG="' + (Join-Path $logs 'install.log') + '"'))
$setup = Start-Process -FilePath $installer -ArgumentList $installArgs -Wait -PassThru -WindowStyle Hidden
if ($setup.ExitCode -ne 0) { throw "Installer exited with code $($setup.ExitCode)." }

try {
    if (-not (Test-Path -LiteralPath $installedExe -PathType Leaf)) { throw 'Installed executable is missing.' }
    if (-not (Test-Path -LiteralPath $launcherExe -PathType Leaf)) { throw 'Installed GUI launcher is missing.' }
    & $installedExe smoke-test
    if ($LASTEXITCODE -ne 0) { throw 'The installed executable failed smoke-test.' }

    $service = Get-Service -Name $serviceName -ErrorAction Stop
    $service.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Running, [TimeSpan]::FromSeconds(30))
    $service.Refresh()
    if ($service.Status -ne 'Running') { throw 'The installed Windows service did not start.' }

    $serviceJson = & $installedExe service status
    if ($LASTEXITCODE -ne 0) { throw 'The installed service status command failed.' }
    $serviceStatus = $serviceJson | ConvertFrom-Json
    if (-not $serviceStatus.installed -or $serviceStatus.state -ne 'running' -or -not $serviceStatus.self_test) {
        throw 'SCM status must confirm the running self-test service, with hardware access disabled.'
    }

    $run = Get-ItemProperty -LiteralPath 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'EliproUPSAgent'
    if ($run.EliproUPSAgent -ne ('"' + $launcherExe + '" --background')) { throw 'Session agent startup command is incorrect.' }
    if (Test-Path -LiteralPath (Join-Path $installRoot 'data')) { throw 'Mutable data must not live in Program Files.' }
    $preservedData = Join-Path $env:ProgramData 'Elipro UPS\data\installer-preserve-check.txt'
    [IO.File]::WriteAllText($preservedData, 'Disposable CI retention check')
    $sentinelAcl = Get-Acl -LiteralPath $preservedData
    $sentinelAcl.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
    Set-Acl -LiteralPath $preservedData -AclObject $sentinelAcl

    # An upgrade must stop and configure the owned service without losing data.
    $upgrade = Start-Process -FilePath $installer -ArgumentList $installArgs -Wait -PassThru -WindowStyle Hidden
    if ($upgrade.ExitCode -ne 0) { throw "Reinstallation exited with code $($upgrade.ExitCode)." }
    if (-not (Test-Path -LiteralPath $preservedData -PathType Leaf)) { throw 'Reinstallation removed retained data.' }
    Write-Host 'Installed executable, service and session startup passed.'
}
finally {
    if (Test-Path -LiteralPath $uninstaller -PathType Leaf) {
        $uninstallArgs = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
            ('/LOG="' + (Join-Path $logs 'uninstall.log') + '"'))
        $remove = Start-Process -FilePath $uninstaller -ArgumentList $uninstallArgs -Wait -PassThru -WindowStyle Hidden
        if ($remove.ExitCode -ne 0) { throw "Uninstaller exited with code $($remove.ExitCode)." }
    }
}

if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) { throw 'The uninstaller left the service registered.' }
$remainingStartup = Get-ItemProperty -LiteralPath 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'EliproUPSAgent' -ErrorAction SilentlyContinue
if ($remainingStartup) { throw 'The uninstaller left session agent startup enabled.' }
if (-not (Test-Path -LiteralPath $preservedData -PathType Leaf)) { throw 'The uninstaller removed user data.' }
Write-Host 'Installer and uninstaller smoke test passed without USB or power actions.'
