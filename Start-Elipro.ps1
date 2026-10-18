param(
    [ValidateRange(1024,65535)][int]$Port = 8765,
    [ValidateSet('auto','native','bridge')][string]$Source = 'auto',
    [switch]$StopForza,
    [switch]$DryRun,
    [switch]$Foreground,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
. (Join-Path $projectRoot 'Monitor-Helpers.ps1')
$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
$pythonPath = if ($pythonCommand -and $pythonCommand.Source -notmatch 'WindowsApps') { $pythonCommand.Source } else { throw 'Instala Python 3.10 o posterior, agrégalo a PATH y vuelve a ejecutar este archivo.' }
$url = "http://127.0.0.1:$Port"
if ($StopForza) {
    if ($Source -eq 'bridge') { throw '-StopForza requiere Source native o auto.' }
    if ($Source -eq 'auto') { $Source = 'native' }
    $oemLauncher = 'C:\Forza\Forza-Startup.ps1'
    if (-not (Test-Path -LiteralPath $oemLauncher)) { throw 'Cierra el colector ForzaTracker antes de usar USB directo; no se encontró su lanzador local.' }
    & $oemLauncher -Command Stop
    # The legacy tray/console can relaunch its collector. Close only that
    # verified installation's UI after the collector has stopped gracefully.
    $forzaProcesses = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like 'C:\Forza\*' })
    $forzaGuiIds = @($forzaProcesses | Where-Object { $_.ExecutablePath -ieq 'C:\Forza\Forza.exe' } | Select-Object -ExpandProperty ProcessId)
    $forzaUiProcesses = @($forzaProcesses | Where-Object { $_.ExecutablePath -ieq 'C:\Forza\Forza.exe' -or $_.ExecutablePath -ieq 'C:\Forza\console\upsTray.exe' -or ($_.ExecutablePath -ieq 'C:\Forza\openJDK\bin\javaw.exe' -and $_.ParentProcessId -in $forzaGuiIds) })
    foreach ($forzaUiProcess in $forzaUiProcesses) {
        $liveUi = Get-Process -Id $forzaUiProcess.ProcessId -ErrorAction SilentlyContinue
        if ($liveUi -and $liveUi.Path -ieq $forzaUiProcess.ExecutablePath) { Stop-Process -Id $liveUi.Id -ErrorAction SilentlyContinue }
    }
}
$existing = $null
try { $existing = Invoke-RestMethod -Uri "$url/api/status" -TimeoutSec 2 } catch { }
if ($existing -and $existing.meta.name -eq 'Elipro UPS') {
    if (($Source -eq 'auto' -or $existing.meta.source -eq $Source) -and (-not $DryRun -or $existing.meta.dry_run -eq $true)) {
        if (-not $NoBrowser) { Start-Process $url }
        Write-Output "Elipro UPS ya está ejecutándose: $url"
        exit 0
    }
    & (Join-Path $projectRoot 'Stop-Elipro.ps1') -Port $Port
} elseif ($existing) {
    throw "El puerto $Port pertenece a otra aplicación. Elipro no la detendrá."
}
$monitorArguments = @('-m','ups_monitor.guardian','--port',"$Port",'--source',$Source)
if ($DryRun) { $monitorArguments += '--dry-run' }
if ($Foreground) {
    Push-Location -LiteralPath $projectRoot
    try { & $pythonPath @monitorArguments } finally { Pop-Location }
    exit $LASTEXITCODE
}
$dataDirectory = Join-Path $projectRoot 'data'
New-Item -ItemType Directory -Force -Path $dataDirectory | Out-Null
$process = Start-Process -FilePath $pythonPath -ArgumentList $monitorArguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $dataDirectory 'server.log') -RedirectStandardError (Join-Path $dataDirectory 'server-error.log') -PassThru
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 200
    if ($process.HasExited) {
        $startupError = (Get-Content -LiteralPath (Join-Path $dataDirectory 'server-error.log') -Tail 3 -ErrorAction SilentlyContinue) -join ' '
        throw "El monitor no pudo iniciarse. $startupError Revisa $dataDirectory\server-error.log"
    }
    try {
        $status = Invoke-RestMethod -Uri "$url/api/status" -TimeoutSec 1
        if ($status.meta.name -eq 'Elipro UPS') {
            if (-not $NoBrowser) { Start-Process $url }
            Write-Output "Elipro UPS activo en segundo plano: $url"
            Write-Output 'Para detenerlo, ejecuta Stop-Elipro.ps1.'
            exit 0
        }
    } catch { }
}
throw "No se pudo confirmar el inicio. Revisa $dataDirectory\server-error.log"
