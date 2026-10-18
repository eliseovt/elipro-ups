# Embedded as -EncodedCommand by Build-Windows.ps1, after the ACL preflight.
# Dot-sourcing only defines the read-only helper for tests; it never queries SCM.
function Get-EliproUpgradePowerCapabilities {
    param([scriptblock]$Executor)

    $executable = [IO.Path]::Combine([Environment]::GetFolderPath('ProgramFiles'), 'Elipro UPS', 'Elipro.exe')
    if (-not $Executor) {
        $Executor = {
            param($Program)
            $output = & $Program service status
            return [pscustomobject]@{ ExitCode = $LASTEXITCODE; StdOut = ($output -join "`n") }
        }
    }
    $response = & $Executor $executable
    if ($response -isnot [pscustomobject] -or $response.ExitCode -isnot [int] -or $response.ExitCode -ne 0) {
        throw 'The installed service status command failed.'
    }
    $json = $response.StdOut
    if ($json -isnot [string] -or [string]::IsNullOrWhiteSpace($json) -or -not $json.TrimStart().StartsWith('{')) {
        throw 'The installed service returned no status object JSON.'
    }
    # Load only the utility module beside the trusted system PowerShell host.
    # User module paths must not influence this elevated, embedded reader.
    Import-Module -Name ([IO.Path]::Combine($PSHOME, 'Modules', 'Microsoft.PowerShell.Utility', 'Microsoft.PowerShell.Utility.psd1')) -ErrorAction Stop
    $status = ConvertFrom-Json -InputObject $json -ErrorAction Stop
    if ($status -isnot [pscustomobject] -or $status.name -cne 'EliproUPS') {
        throw 'The installed service returned an unexpected identity.'
    }
    foreach ($field in @('installed', 'allow_power_actions', 'self_test', 'simulation')) {
        if ($status.$field -isnot [bool]) { throw 'The installed service returned an invalid capability status.' }
    }
    # Versions before the UPS power-off capability only report the Windows gate.
    # Absence of the new field must never opt an existing installation into UPS control.
    $allowUpsPoweroff = $false
    $upsCapability = $status.PSObject.Properties['allow_ups_poweroff']
    if ($null -ne $upsCapability) {
        if ($upsCapability.Value -isnot [bool]) { throw 'The installed service returned an invalid UPS capability.' }
        $allowUpsPoweroff = $upsCapability.Value
    }
    if ($status.allow_power_actions -and (-not $status.installed -or $status.self_test -or $status.simulation)) {
        throw 'The installed service returned conflicting power modes.'
    }
    if ($allowUpsPoweroff -and (-not $status.installed -or -not $status.allow_power_actions -or $status.self_test -or $status.simulation)) {
        throw 'The installed service returned conflicting UPS power modes.'
    }
    return [pscustomobject]@{
        allow_power_actions = $status.installed -and $status.allow_power_actions -and -not $status.self_test -and -not $status.simulation
        allow_ups_poweroff = $allowUpsPoweroff
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    $PSModuleAutoLoadingPreference = 'None'
    try {
        $capabilities = Get-EliproUpgradePowerCapabilities
        if ($capabilities.allow_ups_poweroff) { exit 3 }
        if ($capabilities.allow_power_actions) { exit 1 }
        exit 0
    }
    catch {
        # Never infer permission from a failed query or malformed output.
        [Console]::Error.WriteLine('Cannot determine the installed Elipro power capability.')
        exit 2
    }
}
