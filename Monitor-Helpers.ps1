function Wait-EliproStopped {
    param(
        [ValidateRange(1024,65535)][int]$Port,
        [ValidateRange(1,60)][int]$TimeoutSeconds = 20,
        [int]$MonitorProcessId = 0
    )
    $closeDeadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $portProbe = [System.Net.Sockets.TcpClient]::new()
        try {
            $connectTask = $portProbe.ConnectAsync('127.0.0.1', $Port)
            # Windows can take about two seconds to report a refused local
            # connection. Disposing the probe sooner would never observe it.
            [void]$connectTask.Wait(3000)
        } catch {
            $portError = $_.Exception.GetBaseException()
            if ($portError -is [System.Net.Sockets.SocketException] -and
                $portError.SocketErrorCode -eq [System.Net.Sockets.SocketError]::ConnectionRefused) {
                if ($MonitorProcessId -gt 0) { Wait-EliproProcessExited -TargetProcessId $MonitorProcessId -TimeoutSeconds $TimeoutSeconds }
                return
            }
            throw 'No se pudo confirmar que el monitor terminó. No se iniciará otro lector.'
        } finally {
            $portProbe.Dispose()
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $closeDeadline)
    throw "El monitor sigue ocupando el puerto $Port. Espera y vuelve a intentarlo; no se inició otra instancia."
}

function Wait-EliproProcessExited {
    param([int]$TargetProcessId, [ValidateRange(1,60)][int]$TimeoutSeconds = 20)
    if ($TargetProcessId -le 0) { return }
    $targetProcess = Get-Process -Id $TargetProcessId -ErrorAction SilentlyContinue
    if ($targetProcess -and -not $targetProcess.WaitForExit($TimeoutSeconds * 1000)) {
        throw 'El proceso de Elipro todavía no terminó. No se iniciará otro lector.'
    }
}

function Get-EliproGuardianInfo {
    param([string]$DataDirectory, [int]$Port)
    $metadataPath = Join-Path $DataDirectory 'guardian.json'
    try {
        $metadataFile = Get-Item -LiteralPath $metadataPath -ErrorAction Stop
        if ($metadataFile.Length -gt 8192) { return $null }
        $guardianInfo = Get-Content -LiteralPath $metadataPath -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($guardianInfo.port -ne $Port -or $guardianInfo.pid -le 0 -or $guardianInfo.session_id -notmatch '^[0-9a-f]{32}$') { return $null }
        $guardianProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$guardianInfo.pid)" -ErrorAction Stop
        if (-not $guardianProcess -or $guardianProcess.CommandLine -notmatch '(?:^|\s)-m\s+ups_monitor\.guardian(?:\s|$)') { return $null }
        return $guardianInfo
    } catch { return $null }
}

function Request-EliproGuardianStop {
    param([string]$DataDirectory, [object]$GuardianInfo)
    $markerPath = Join-Path $DataDirectory 'guardian.stop'
    $temporaryMarker = Join-Path $DataDirectory ".guardian.stop.powershell.$PID.tmp"
    $markerContent = @{guardian_pid=[int]$GuardianInfo.pid; session_id=[string]$GuardianInfo.session_id} | ConvertTo-Json -Compress
    [System.IO.File]::WriteAllText($temporaryMarker, $markerContent, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporaryMarker -Destination $markerPath -Force
}
