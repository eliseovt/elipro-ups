param([ValidateRange(1024,65535)][int]$Port = 8765)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Monitor-Helpers.ps1')
$url = "http://127.0.0.1:$Port"
$dataDirectory = Join-Path $PSScriptRoot 'data'
$guardianInfo = Get-EliproGuardianInfo -DataDirectory $dataDirectory -Port $Port
$status = $null
try { $status = Invoke-RestMethod -Uri "$url/api/status" -TimeoutSec 3 } catch { }
if (-not $status -and -not $guardianInfo) {
    throw 'No hay un monitor accesible y no se pudo confirmar el estado del lanzador. No se puede garantizar que Elipro haya quedado detenido.'
}
if ($status -and $status.meta.name -ne 'Elipro UPS') {
    if (-not $guardianInfo) { throw 'El puerto no corresponde a Elipro UPS.' }
    Request-EliproGuardianStop -DataDirectory $dataDirectory -GuardianInfo $guardianInfo
    Wait-EliproProcessExited -TargetProcessId ([int]$guardianInfo.pid)
    Write-Output 'El lanzador de Elipro se detuvo. El puerto pertenece a otra aplicación y no se modificó.'
    return
}
# Stop the guardian first, including when its child is between crash retries.
# Its own PID/session marker is ignored by any later guardian session.
if ($guardianInfo) { Request-EliproGuardianStop -DataDirectory $dataDirectory -GuardianInfo $guardianInfo }
if ($status -and (-not $guardianInfo -or $status.health.process.pid -eq $guardianInfo.child_pid)) {
    try {
        Invoke-RestMethod -Method Post -Uri "$url/api/service/stop" -ContentType 'application/json' -Body '{}' -Headers @{'X-UPS-Token'=$status.csrf_token} -TimeoutSec 3 | Out-Null
    } catch { if (-not $guardianInfo) { throw } }
}
if ($guardianInfo) { Wait-EliproProcessExited -TargetProcessId ([int]$guardianInfo.pid) }
$monitorProcessId = if ($status -and $status.health.process.pid) { [int]$status.health.process.pid } elseif ($guardianInfo -and $guardianInfo.child_pid) { [int]$guardianInfo.child_pid } else { 0 }
Wait-EliproStopped -Port $Port -MonitorProcessId $monitorProcessId
Write-Output 'Monitor detenido. El panel y la protección automática ya no están activos.'
