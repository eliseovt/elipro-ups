[CmdletBinding()]
param([switch]$Simulation)

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
$serviceTestArgument = if ($Simulation) { '/SERVICESIMULATION=1' } else { '/SERVICESELFTEST=1' }
if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
    throw 'The disposable runner already contains an Elipro service; refusing to replace it.'
}
New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Assert-InstallerRejects([string]$Label, [string[]]$ExtraArguments) {
    $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', $serviceTestArgument,
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

# Both modes avoid USB and desktop UI. Simulation additionally runs the real
# service, monitor, HTTP API, protection engine and durable history in Session 0.
$installArgs = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', $serviceTestArgument,
    ('/LOG="' + (Join-Path $logs 'install.log') + '"'))
$setup = Start-Process -FilePath $installer -ArgumentList $installArgs -Wait -PassThru -WindowStyle Hidden
if ($setup.ExitCode -ne 0) { throw "Installer exited with code $($setup.ExitCode)." }

try {
    if (-not (Test-Path -LiteralPath $installedExe -PathType Leaf)) { throw 'Installed executable is missing.' }
    if (-not (Test-Path -LiteralPath $launcherExe -PathType Leaf)) { throw 'Installed GUI launcher is missing.' }
    & $installedExe smoke-test
    if ($LASTEXITCODE -ne 0) { throw 'The installed executable failed smoke-test.' }
    $signingReportPath = Join-Path $repoRoot 'dist\installer\signing-status.json'
    if (-not (Test-Path -LiteralPath $signingReportPath -PathType Leaf)) { throw 'Build signing metadata is missing.' }
    $signingReport = Get-Content -LiteralPath $signingReportPath -Raw | ConvertFrom-Json
    if ($signingReport.signed -isnot [bool]) { throw 'Build signing metadata has an invalid signed flag.' }
    $installedSignatures = @()
    if ($signingReport.signed -eq $true) {
        foreach ($binary in @($installedExe, $launcherExe, $uninstaller)) {
            $signature = Get-AuthenticodeSignature -LiteralPath $binary
            if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate -or
                $signature.SignerCertificate.Thumbprint -ne $signingReport.certificate_thumbprint -or
                -not $signature.TimeStamperCertificate) { throw "Installed signature validation failed: $binary" }
            $installedSignatures += [pscustomobject]@{ file = [IO.Path]::GetFileName($binary);
                status = 'Valid'; timestamped = $true; certificate_thumbprint = $signature.SignerCertificate.Thumbprint }
        }
    }
    [ordered]@{ signed = [bool]$signingReport.signed; verified_installed_files = $installedSignatures } |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $logs 'installed-signatures.json') -Encoding utf8

    $service = Get-Service -Name $serviceName -ErrorAction Stop
    $service.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Running, [TimeSpan]::FromSeconds(30))
    $service.Refresh()
    if ($service.Status -ne 'Running') { throw 'The installed Windows service did not start.' }

    $serviceJson = & $installedExe service status
    if ($LASTEXITCODE -ne 0) { throw 'The installed service status command failed.' }
    $serviceStatus = $serviceJson | ConvertFrom-Json
    if (-not $serviceStatus.installed -or $serviceStatus.state -ne 'running' -or $serviceStatus.allow_power_actions) {
        throw 'SCM must confirm a running service without real power capability.'
    }
    if ($Simulation) {
        if (-not $serviceStatus.simulation -or $serviceStatus.self_test) { throw 'SCM did not register simulation mode.' }
    }
    elseif (-not $serviceStatus.self_test) { throw 'SCM did not register self-test mode.' }

    if ($Simulation) {
        function Invoke-SimulationRequest([string]$Path, [string]$Method = 'Get', [string]$Body = '', [hashtable]$Headers = @{}) {
            if ($Path -notin @('/api/status', '/api/policy', '/api/history?hours=1', '/api/events/search?limit=200')) {
                throw 'The simulation helper only accepts fixed local test endpoints.'
            }
            $requestDeadline = [DateTime]::UtcNow.AddSeconds(10)
            do {
                try {
                    $parameters = @{ Uri = 'http://127.0.0.1:8765' + $Path; Method = $Method;
                        Headers = $Headers; TimeoutSec = 5; NoProxy = $true }
                    if ($Method -eq 'Post') { $parameters.Body = $Body; $parameters.ContentType = 'application/json' }
                    return Invoke-RestMethod @parameters
                }
                catch {
                    $statusCode = [int]$_.Exception.Response.StatusCode
                    # A background critical batch temporarily owns SQLite. All
                    # POSTs here replace policy idempotently; never retry other
                    # failures or bypass source/capability assertions.
                    $retryable = $statusCode -eq 503 -or ($Path -eq '/api/status' -and $statusCode -eq 0)
                    if (-not $retryable -or [DateTime]::UtcNow -ge $requestDeadline) { throw }
                    Start-Sleep -Milliseconds 250
                }
            } while ($true)
        }
        function Get-SimulationStatus {
            return Invoke-SimulationRequest '/api/status'
        }
        function Wait-SimulationConnected {
            $readyDeadline = [DateTime]::UtcNow.AddSeconds(30)
            do {
                try { $readyStatus = Get-SimulationStatus } catch { $readyStatus = $null }
                if ($readyStatus -and $readyStatus.connection.state -eq 'connected') { return $readyStatus }
                Start-Sleep -Milliseconds 500
            } while ([DateTime]::UtcNow -lt $readyDeadline)
            throw 'The simulated service did not publish fresh telemetry.'
        }
        function Assert-SimulationSafe($Status, [bool]$AllowEnabled = $false) {
            if ($Status.meta.source -ne 'simulation' -or -not $Status.meta.simulation_source -or
                -not $Status.meta.dry_run -or $Status.meta.runtime_mode -ne 'service') {
                throw 'The installed monitor must identify simulated telemetry, Session 0 mode and blocked OS actions.'
            }
            if (-not $AllowEnabled -and $Status.policy.enabled) { throw 'Automatic protection must remain disabled outside the simulation scenario.' }
            if ($Status.health.process.pid -ne $serviceStatus.pid) { throw 'The monitor must run inside the registered service process.' }
        }
        $serviceProcess = Get-Process -Id $serviceStatus.pid -ErrorAction Stop
        if ($serviceProcess.SessionId -ne 0) { throw 'The simulated monitor is not running in Session 0.' }
        $serviceConfiguration = Get-CimInstance -ClassName Win32_Service -Filter "Name='EliproUPS'"
        if ($serviceConfiguration.StartName -notin @('LocalSystem', 'NT AUTHORITY\SYSTEM')) {
            throw 'The simulation service must use the LocalSystem account.'
        }
        $snapshot = Wait-SimulationConnected
        Assert-SimulationSafe $snapshot
        $headers = @{ 'X-UPS-Token' = $snapshot.csrf_token; Origin = 'http://127.0.0.1:8765' }
        $safePolicy = @{ enabled = $true; on_battery_seconds = 86400; low_battery_percent = 0;
            min_runtime_seconds = 0; countdown_seconds = 10; action = 'shutdown' } | ConvertTo-Json -Compress
        $observedPhases = [Collections.Generic.HashSet[string]]::new()
        $sawCountdown = $false
        try {
            # This is intentionally an enabled POLICY with independently forced
            # dry-run source/capability gates; it cannot request a Windows action.
            $null = Invoke-SimulationRequest '/api/policy' 'Post' $safePolicy $headers
            $scenarioDeadline = [DateTime]::UtcNow.AddSeconds(90)
            do {
                $snapshot = Get-SimulationStatus
                Assert-SimulationSafe $snapshot $true
                if ($snapshot.sample) { $null = $observedPhases.Add([string]$snapshot.sample.raw_status) }
                if ($snapshot.protection.state -eq 'countdown' -and $snapshot.protection.simulation) { $sawCountdown = $true }
                $kinds = @($snapshot.events | ForEach-Object { $_.kind })
                $complete = $sawCountdown -and ('simulation' -in $kinds) -and ('battery_low' -in $kinds) -and
                    ('power_lost' -in $kinds) -and ('power_restored' -in $kinds) -and
                    $snapshot.sample.on_battery -eq $false
                if ($complete) { break }
                Start-Sleep -Seconds 1
            } while ([DateTime]::UtcNow -lt $scenarioDeadline)
            if (-not $complete) { throw 'The real service did not complete simulated battery, countdown, blocked action and recovery.' }
            if ('policy_action_started' -in $kinds -or 'policy_action_failed' -in $kinds) { throw 'Simulation must not record a real Windows action.' }
        }
        finally {
            $null = Invoke-SimulationRequest '/api/policy' 'Post' '{"enabled":false}' $headers
        }
        $snapshot = Get-SimulationStatus
        Assert-SimulationSafe $snapshot
        $historyResponse = Invoke-SimulationRequest '/api/history?hours=1'
        $beforeHistory = @($historyResponse.samples)
        $beforeArchive = Invoke-SimulationRequest '/api/events/search?limit=200'
        if ($beforeHistory.Count -lt 3 -or $beforeArchive.total -lt 4) { throw 'Simulation did not persist measurements and critical events.' }
        $report = [ordered]@{ source = 'simulation'; session_id = $serviceProcess.SessionId;
            service_account = $serviceConfiguration.StartName; dry_run = $true;
            automatic_shutdown_enabled = $false; countdown_observed = $sawCountdown;
            phases = @($observedPhases); event_kinds = @($kinds | Select-Object -Unique);
            samples_before_upgrade = $beforeHistory.Count; events_before_upgrade = $beforeArchive.total }
        $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $logs 'simulation-service.json') -Encoding utf8
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
    if ($Simulation) {
        $serviceJson = & $installedExe service status
        if ($LASTEXITCODE -ne 0) { throw 'Service status failed after upgrade.' }
        $serviceStatus = $serviceJson | ConvertFrom-Json
        if (-not $serviceStatus.simulation -or $serviceStatus.allow_power_actions) { throw 'Upgrade changed safe simulation registration.' }
        $snapshot = Wait-SimulationConnected
        Assert-SimulationSafe $snapshot
        $historyResponse = Invoke-SimulationRequest '/api/history?hours=1'
        $afterHistory = @($historyResponse.samples)
        $afterArchive = Invoke-SimulationRequest '/api/events/search?limit=200'
        if ($afterHistory.Count -lt $beforeHistory.Count -or $afterArchive.total -lt $beforeArchive.total) {
            throw 'Upgrade lost persisted simulation history.'
        }
    }
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
if ($Simulation) {
    $databasePath = Join-Path $machineRoot 'data\monitor.sqlite3'
    if (-not (Test-Path -LiteralPath $databasePath -PathType Leaf)) { throw 'The uninstaller removed the simulation database.' }
    $databaseCheck = @'
import json, sqlite3, sys
from pathlib import Path
database = sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True)
try:
    samples = [json.loads(row[0]) for row in database.execute("SELECT payload FROM samples")]
    kinds = {row[0] for row in database.execute("SELECT kind FROM events")}
    policy = json.loads(database.execute("SELECT value FROM settings WHERE key='policy'").fetchone()[0])
    assert len(samples) >= 3 and all(item['raw_status'].startswith('SIMULATION/') for item in samples)
    assert {'power_lost', 'battery_low', 'simulation', 'power_restored'} <= kinds
    assert 'policy_action_started' not in kinds and not policy['enabled']
    print(json.dumps({'sample_count': len(samples), 'event_kinds': sorted(kinds), 'policy_enabled': False}))
finally:
    database.close()
'@
    $databaseReport = & python -c $databaseCheck $databasePath
    if ($LASTEXITCODE -ne 0) { throw 'The durable simulation database failed validation after uninstall.' }
    $databaseReport | Set-Content -LiteralPath (Join-Path $logs 'durable-database.json') -Encoding utf8
}
Write-Host 'Installer and uninstaller smoke test passed without USB or power actions.'
