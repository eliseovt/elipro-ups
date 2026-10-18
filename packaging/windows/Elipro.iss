; Compile through Build-Windows.ps1. No local history or configuration is input.
#ifndef AppVersion
  #error AppVersion must be provided by the build script.
#endif
#ifndef SourceDir
  #error SourceDir must point to the PyInstaller onedir output.
#endif
#ifndef OutputDir
  #error OutputDir must point to the installer output directory.
#endif
#ifndef SecurityPreflight
  #error SecurityPreflight must be embedded by the build script.
#endif

[Setup]
AppId={{34C2FD4D-76D9-47E0-A699-21209C29CF5A}
AppName=Elipro UPS
AppVersion={#AppVersion}
AppPublisher=Elipro UPS contributors
AppComments=Monitor independiente para UPS, con servicio de Windows y panel local.
DefaultDirName={autopf}\Elipro UPS
DisableDirPage=yes
UsePreviousAppDir=no
DefaultGroupName=Elipro UPS
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible and not arm64
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=Elipro-Setup-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile={#SourceDir}\_internal\static\assets\elipro.ico
UninstallDisplayIcon={app}\Elipro.exe
UninstallDisplayName=Elipro UPS
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
RestartIfNeededByRun=no
#ifdef EnableSigning
SignTool=EliproSigning
SignedUninstaller=yes
#else
SignedUninstaller=no
#endif

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el escritorio"; GroupDescription: "Accesos directos:"; Flags: unchecked

[Dirs]
; Created atomically with protected ACLs in the security preflight. Retain data.
Name: "{commonappdata}\Elipro UPS\data"; Flags: uninsneveruninstall

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Elipro UPS"; Filename: "{app}\EliproLauncher.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Elipro UPS"; Filename: "{app}\EliproLauncher.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
; Interactive notifications run in each user's session, separately from the service.
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "EliproUPSAgent"; ValueData: """{app}\EliproLauncher.exe"" --background"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\EliproLauncher.exe"; Parameters: "--background"; Flags: nowait runasoriginaluser skipifsilent
Filename: "{app}\EliproLauncher.exe"; Description: "Abrir el panel de Elipro UPS"; Flags: postinstall nowait runasoriginaluser skipifsilent unchecked

[Code]
function RunServiceCommand(const Arguments: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{app}\Elipro.exe'), 'service ' + Arguments,
    ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if Result then
    Result := ResultCode = 0;
  if not Result then
    Log('Elipro service command failed: ' + Arguments);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  PreviousRedirection: Boolean;
begin
  Result := '';
  if (ExpandConstant('{param:SERVICESELFTEST|0}') = '1') and
    (ExpandConstant('{param:SERVICESIMULATION|0}') = '1') then
  begin
    Result := 'SERVICESELFTEST y SERVICESIMULATION son modos distintos y no pueden combinarse.';
    Exit;
  end;
  if CompareText(RemoveBackslash(ExpandConstant('{app}')),
    RemoveBackslash(ExpandConstant('{autopf}\Elipro UPS'))) <> 0 then
  begin
    Result := 'El servicio solo puede instalarse en Program Files\Elipro UPS. No se admite /DIR personalizado.';
    Exit;
  end;
  if CheckForMutexes('Global\EliproUPS.Guardian') then
  begin
    Result := 'Elipro sigue ejecutándose desde su carpeta de desarrollo. La instalación no lo cerrará. Finaliza la observación y cierra esa instancia antes de instalar.';
    Exit;
  end;
  // Execute embedded code through the trusted Windows PowerShell binary. No
  // temporary script or user-controlled application binary runs elevated.
  PreviousRedirection := EnableFsRedirection(False);
  try
    if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {#SecurityPreflight}',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
    begin
      Result := 'La carpeta de Elipro ya existe con permisos inseguros, herencia no protegida o enlaces. No se modificaron sus datos. Revisa esa carpeta con un administrador antes de instalar.';
      Exit;
    end;
  finally
    EnableFsRedirection(PreviousRedirection);
  end;
  if FileExists(ExpandConstant('{app}\Elipro.exe')) then
  begin
    // End this session's notifications before the planned service outage.
    // An unexpected service loss still warns through the running agent.
    if not Exec(ExpandConstant('{app}\Elipro.exe'), 'agent --stop',
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) or
      (ResultCode <> 0) then
      Result := 'No se pudo cerrar el agente de avisos de Elipro. Ciérralo e inténtalo de nuevo.';
    if Result = '' then
      if not RunServiceCommand('stop') then
        Result := 'No se pudo detener el servicio de Elipro. Ciérralo desde Servicios de Windows e inténtalo de nuevo.';
  end;
  if Result = '' then
    if CheckForMutexes('Global\EliproUPS.Monitor,Global\EliproUPS.Guardian') then
      Result := 'Ya hay otro monitor de Elipro en ejecución. La instalación no lo cerrará. Finaliza la observación y cierra esa instancia antes de instalar; así conservarás la prueba actual.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Arguments: String;
begin
  if CurStep = ssPostInstall then
  begin
    Arguments := 'install --data-dir "' + ExpandConstant('{commonappdata}\Elipro UPS\data') + '"';
    // Only the disposable CI runner uses this explicit no-hardware mode.
    if ExpandConstant('{param:SERVICESELFTEST|0}') = '1' then
      Arguments := Arguments + ' --self-test';
    if ExpandConstant('{param:SERVICESIMULATION|0}') = '1' then
      Arguments := Arguments + ' --simulate';
    if not RunServiceCommand(Arguments) then
      RaiseException('No se pudo registrar el servicio de Elipro. Consulta el registro de instalación.');
    if not RunServiceCommand('start') then
      RaiseException('No se pudo iniciar el servicio de Elipro. Consulta el registro de instalación.');
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    if not Exec(ExpandConstant('{app}\Elipro.exe'), 'agent --stop',
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) or
      (ResultCode <> 0) then
      RaiseException('No se pudo cerrar el agente de avisos de Elipro; la desinstalación se ha detenido.');
    if not RunServiceCommand('stop') then
      RaiseException('No se pudo detener el servicio de Elipro; la desinstalación se ha detenido.');
    if not RunServiceCommand('remove') then
      RaiseException('No se pudo quitar el servicio de Elipro; la desinstalación se ha detenido.');
  end;
end;
