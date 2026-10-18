# Elipro UPS

Monitor local para Windows con panel web, avisos de escritorio, historial y reglas de apagado o hibernación. Su lector USB está diseñado para el **Forza NT-2402PRO** y utiliza consultas de estado; no envía órdenes para apagar el UPS, iniciar descargas ni modificar sus parámetros.

Elipro es un proyecto independiente en desarrollo. No es software oficial de Forza ni tiene certificación del fabricante. La compatibilidad con otros modelos o revisiones de firmware no está comprobada.

**El apagado automático está desactivado por defecto.** Windows debe permanecer despierto para vigilar el UPS. El instalador registra el servicio con el bloqueo `--dry-run`: las acciones reales de energía están bloqueadas, incluso si se activa una regla en el panel. La capacidad de ejecutar acciones requiere una modificación administrativa explícita del registro del servicio; es independiente de activar una regla.

## Funciones

- Lectura de alimentación de red/batería, voltajes de entrada y salida, frecuencia, carga del UPS, voltaje de batería y señales de batería baja o falla.
- Seis secciones: **Resumen, Incidentes, Historial, Protección, Avisos y Sistema**; temas claro y oscuro con selector sol/luna. Resumen e Historial comparten el gráfico completo y los eventos recientes, conservando sus filtros al navegar.
- Avisos de Windows, sonido e icono de bandeja aunque se cierre el navegador.
- Avisos eléctricos retenidos durante bloqueo o desconexión de sesión, con acuse de aceptación nativa y recuperación del contexto al reanudar.
- Historial local SQLite, gráficas, incidentes, filtros, exportación CSV, diagnóstico y respaldo ZIP.
- Reglas deterministas con cuenta atrás cancelable; simulación de la cuenta sin ejecutar acciones de energía.
- Separación del historial, la lectura, los avisos y la protección; exclusión de instancias y recuperación supervisada del lector.
- Observación prolongada de recursos y errores con informe local para revisión.

No requiere cuentas ni servicios de nube para funcionar. El servidor escucha únicamente en `127.0.0.1`; no está diseñado para exponerlo a una red.

## Instalador de Windows x64

El mismo instalador admite procesadores AMD e Intel x64. Incluye el runtime de Python; no requiere que el usuario instale Python por separado. La instalación necesita permisos de administrador.

El diseño separa dos procesos:

- **Servicio `EliproUPS`:** arranca con Windows, ejecuta el monitor sin escritorio y es el único que abre el USB. Usa `Program Files\Elipro UPS` para el programa y `ProgramData\Elipro UPS\data` para los datos. Se registra bajo la cuenta LocalSystem, con recuperación ante fallos.
- **Agente de sesión:** muestra la bandeja y los avisos al iniciar sesión. `Elipro.exe agent --open-panel` abre también el panel. No abre otro lector USB; se comunica con el servicio por la interfaz local.

El servicio vigila aunque no haya una sesión iniciada; los avisos visuales necesitan una sesión interactiva. La protección se evalúa dentro del servicio, en Sesión 0, y no depende del agente ni del navegador. Las pruebas del instalador en una máquina virtual no validan el USB físico, la autonomía, la suspensión ni un apagado real. Antes de usar la protección, hay que completar esas comprobaciones en el equipo de destino.

La desinstalación detiene y elimina el servicio y el inicio del agente, pero conserva los datos locales. No se importan historiales ni ajustes personales dentro del instalador.

### GitHub Actions

El workflow **Windows x64 installer** ejecuta las pruebas, empaqueta el programa en directorio (`onedir`), genera el instalador y comprueba instalación, actualización y desinstalación en el runner desechable de Windows. La prueba inicia el monitor completo como servicio en Sesión 0 con un UPS simulado: verifica API, transiciones, historial y motor de protección sin USB ni escritorio interactivo. Las acciones reales están bloqueadas en la simulación.

Se ejecuta al subir cambios a `main`, en etiquetas `v*`, en pull requests y desde **Actions → Windows x64 installer → Run workflow**. Al finalizar correctamente, el artefacto `Elipro-Windows-x64-<número>` contiene:

- `Elipro-Setup-x64.exe`
- `Elipro-Setup-x64.exe.sha256`
- `signing-status.json`, que distingue compilación firmada y sin firmar.

Los artefactos se conservan 14 días. El workflow no publica automáticamente una GitHub Release.

### Firma digital opcional

La compilación normal no requiere certificado y se identifica como **sin firmar**. La firma auténtica necesita un certificado de firma de código válido con su clave privada. No se crean certificados de prueba ni se modifican almacenes de confianza para aparentar una firma válida. Una firma no garantiza eliminar advertencias de reputación de SmartScreen ni detecciones de antivirus.

Para compilar con un certificado ya instalado en el almacén personal de Windows:

```powershell
.\packaging\windows\Build-Windows.ps1 -SigningCertificateThumbprint '<huella de 40 caracteres hexadecimales>' -RequireSigning
```

`-RequireSigning` rechaza una compilación si falta el certificado, la herramienta, el sellado de tiempo o una verificación válida. Sin esa opción, un intento de firma configurado que falla tampoco se rebaja silenciosamente a una distribución sin firmar. Las contraseñas y el archivo PFX no se pasan como argumentos a SignTool. El SHA256 del instalador se calcula después de firmar.

En GitHub, la firma usa un job separado y manual desde `main` o una etiqueta `v*`, después del job normal aprobado. Antes de utilizarlo, configura el entorno **release-signing** con revisores requeridos y restricciones para esas referencias. Guarda allí los secretos `ELIPRO_SIGNING_PFX_BASE64` y `ELIPRO_SIGNING_PFX_PASSWORD`; no los incluyas en archivos ni mensajes. La variable opcional `ELIPRO_SIGNING_TIMESTAMP_URL` permite elegir el servidor HTTPS de sellado RFC3161.

En **Run workflow**, activa `sign_release` solo cuando ese entorno y un certificado válido estén preparados. El job importa temporalmente el certificado en el almacén personal del runner, firma y verifica, y elimina el PFX y la clave importada al terminar. Las pull requests y compilaciones normales no reciben esos secretos. El artefacto firmado lleva `signed` en su nombre.

### Compilar localmente

Requisitos de compilación: Windows x64, Python 3.12 x64, Node.js para las pruebas e Inno Setup 6. Desde la raíz del proyecto:

```powershell
python -m pip install --require-hashes --only-binary=:all: -r requirements-build.txt
python -m unittest discover -s tests -v
node --test tests/test_notifications_ui.js
powershell -NoProfile -ExecutionPolicy Bypass -File packaging/windows/Build-Windows.ps1
```

El resultado queda en `dist/installer/`. El ejecutable incluye las licencias del runtime y las herramientas incorporadas, separadas de la licencia pendiente del código de Elipro.

## Ejecutar desde el código

Requisitos: Windows x64, Python 3.10 o posterior y el cable USB de datos del UPS. La aplicación usa la biblioteca estándar de Python y las API de Windows; no necesita instalar dependencias Python para ejecutarse desde el código.

Antes de usar USB directo, cierra el lector original de ForzaTracker para que un solo programa acceda al UPS. Elipro no sustituye el controlador HID de Windows.

Antes de cada lectura, Elipro consulta las tablas UDP IPv4 e IPv6 de Windows para comprobar el puerto local del colector. Solo permite USB si ambas consultas completas confirman su ausencia. Un puerto ocupado o una consulta inconclusa mantienen el bloqueo; `VP_LINK` se usa únicamente para reconocer una respuesta positiva de ForzaTracker. La ausencia no depende de recibir un error ICMP ni se guarda en caché. La causa y los límites se explican en [el diagnóstico de arranque](docs/startup-communication.md).

Desde PowerShell, en la carpeta del proyecto:

```powershell
.\Start-Elipro.ps1 -Source native -DryRun
```

Abre <http://127.0.0.1:8765>. `-DryRun` bloquea las acciones reales de apagado e hibernación, incluso si se activa una regla en el panel. Es el modo recomendado para comenzar y verificar la lectura, las alertas y las simulaciones.

Cerrar la pestaña mantiene el monitor y sus avisos en ejecución. Para detenerlos:

```powershell
.\Stop-Elipro.ps1
```

El lanzador incluye un supervisor de proceso durante la sesión actual: intenta recuperar un cierre inesperado con pausas limitadas. Una salida solicitada desde el panel, la bandeja o el script no provoca un reinicio. No crea servicios ni tareas de inicio.

Este modo desde el código depende de la sesión en la que se inició; no ofrece la cobertura del servicio antes de iniciar sesión o tras cerrarla. La opción `-StopForza` del script está reservada a una instalación local concreta del lanzador original y no es un mecanismo universal para detener ForzaTracker.

También se puede ejecutar en una consola, sin el supervisor externo:

```powershell
python -m ups_monitor --source native --dry-run
```

`--source bridge` es un modo alternativo que obtiene telemetría del servidor local de ForzaTracker y requiere que este esté en ejecución. No debe confundirse con la lectura USB propia. La selección `auto` se resuelve al iniciar y no cambia de fuente durante un corte.

## Lecturas y límites del hardware

El lector nativo acepta el dispositivo HID `0665:5161` con el formato de reporte y la respuesta binaria QS/T compatibles con la implementación. El sondeo habitual solo envía `QS` seguido de retorno de carro; el diagnóstico explícito añade únicamente las consultas `BT?` y `BL?`, con una lista permitida separada.

Esta respuesta proporciona **voltaje de batería y señal de batería baja, pero no un porcentaje medido de batería, minutos restantes, temperatura ni watts**. La tarjeta separa el porcentaje de carga y el voltaje; no convierte el porcentaje de carga del UPS en consumo eléctrico.

El porcentaje nativo derivado se identifica como **Carga estimada** y utiliza un perfil versionado para el NT-2402PRO. Su cálculo y filtrado se contrastan con observaciones del programa del fabricante; no equivalen a una medición de salud ni garantizan autonomía restante. Los datos incompatibles o no disponibles se muestran como tales, nunca como 0% por defecto. La procedencia, las capturas y los límites se documentan en [la investigación de batería](docs/battery-percentage-research.md).

Las estimaciones usan los campos aditivos `battery_percent_estimate` y `battery_estimate_method`; `battery_percent` conserva su significado de porcentaje reportado por una fuente compatible. Se guardan únicamente desde su incorporación: no se recalcula el historial previo. El CSV mantiene las columnas existentes en su orden y agrega los dos campos al final. Las estimaciones no disparan apagados, eventos de batería baja ni avisos basados en porcentaje medido.

Los porcentajes disponibles en el modo puente son los que informa ForzaTracker; no son una medición independiente de salud de la batería. Los valores que ese servidor no proporciona de forma válida se muestran como no disponibles.

El diagnóstico explícito de batería permite observar `QS`, `BT?` y `BL?`, sin crear un monitor ni ejecutar acciones de energía:

```powershell
python -m ups_monitor.battery_diagnostics --samples 3 --interval 1 --label network
# Con el ejecutable instalado:
Elipro.exe battery-diagnostic --samples 3 --interval 1 --label network
```

Este diagnóstico requiere que el servicio de Elipro y el lector de ForzaTracker estén detenidos; rechaza lectores simultáneos y no los detiene por su cuenta. Los comandos adicionales permanecen fuera del sondeo habitual: una respuesta no compatible no se interpreta como porcentaje. Las etiquetas `network`, `battery` y `recovery` describen observaciones con cambios eléctricos manuales; no cortan el suministro ni simulan estados.

## Protección automática

Se mantiene desactivada hasta que el usuario la configura y activa expresamente. Los ajustes guardados se conservan al reiniciar; `--dry-run` añade un bloqueo de acciones reales para toda esa ejecución.

El servicio instalado conserva ese bloqueo salvo que un administrador lo registre expresamente mediante `Elipro.exe service install --allow-power-actions`, con el servicio detenido y después de validar el equipo. Esta operación no activa la política guardada. Volver a registrarlo sin esa opción restaura el bloqueo; `service status` informa la capacidad configurada. No debe usarse la opción durante pruebas ni combinarse con `--self-test`.

La evaluación exige datos recientes, alimentación por batería confirmada y un lector/supervisor sanos. En USB directo se utilizan el tiempo continuo en batería y la señal real de batería baja; los umbrales de porcentaje y autonomía solo participan cuando existe una lectura compatible.

Volver a la red, perder la comunicación o recibir datos inválidos cancela una cuenta pendiente. Cancelarla manualmente suprime la acción hasta que vuelva la red y ocurra otro corte. La cuenta se mantiene visible en todas las secciones.

Al vencer una cuenta real, Elipro solicita el apagado o la hibernación de Windows. El apagado no fuerza el cierre de aplicaciones: estas pueden bloquearlo y Elipro no guarda documentos. La hibernación necesita estar disponible en Windows. No se cambia automáticamente esa configuración.

Antes de activar acciones reales, hay que validar la autonomía con la carga habitual, el tiempo de cierre de Windows, los avisos y la cancelación. Una prueba breve de batería no permite extrapolar varios minutos de respaldo. Las pruebas automatizadas no certifican la seguridad del equipo ni sustituyen esas comprobaciones.

## Historial y diagnóstico

Al ejecutar desde el código, los datos se guardan en `data/`; el servicio instalado utiliza la carpeta de ProgramData indicada arriba. La retención predeterminada es de 30 días. Las exportaciones incluyen lo que todavía se conserva; no recuperan información ya eliminada por retención.

Los errores de SQLite se muestran por separado y no deben interrumpir una lectura válida ni los avisos. Las muestras normales se guardan por lotes de hasta 32 registros o 30 segundos; los eventos críticos solicitan volcado inmediato. Una cola limitada conserva registros mientras la escritura falla; si se llena, puede descartar muestras y lo informa. Los datos pendientes en memoria se pierden al finalizar el proceso.

La adquisición y el procesamiento usan hilos separados y un ring buffer limitado. Los retrasos invalidan la continuidad de la protección y nunca autorizan una acción con una lectura antigua. Los límites y garantías se detallan en [la arquitectura de confiabilidad](docs/reliability-architecture.md).

El diagnóstico usa una lista explícita de campos y omite tokens, variables de entorno y rutas del dispositivo. El respaldo contiene la base y los ajustes locales: revísalo antes de compartirlo. No reemplaces una base mientras Elipro la está utilizando.

Para iniciar una observación de estabilidad con acciones reales bloqueadas:

```powershell
.\Start-Elipro.ps1 -Source native -DryRun -ObserveHours 48
```

También se puede gestionar desde **Sistema**. Completar el tiempo no equivale a aprobarla: el informe debe revisarse. El tiempo sin monitor o con Windows suspendido no cuenta como protección verificada.

## Desarrollo y pruebas

Las pruebas usan API de Windows y notificadores simulados, bases temporales y callbacks de acciones de energía; no necesitan un UPS conectado ni deben apagar o hibernar la PC.

```powershell
python -m unittest discover -s tests -v
node --check static/app.js
node --test tests/test_notifications_ui.js
```

Node.js solo se necesita para las pruebas de interfaz. Las comprobaciones aisladas del motor también pueden ejecutarse con:

```powershell
python -m ups_monitor.validation --output .test-data/controlled-validation.json
```

Para consultar metadatos HID sin enviar una consulta QS:

```powershell
python -m ups_monitor --usb-info
```

`--diagnose` realiza una lectura real y termina; requiere el bloqueo exclusivo, por lo que no puede ejecutarse junto con el monitor habitual.

## Publicación y datos locales

El repositorio contiene código y pruebas. Se excluyen el historial, los ajustes, informes, registros, respaldos, credenciales y capturas locales con telemetría. No publiques el contenido de `data/` ni archivos de diagnóstico o respaldo sin revisarlos.

Todavía no se ha elegido una licencia de distribución para el código del proyecto. Que el repositorio sea público no concede por sí solo los permisos de una licencia open source. Las marcas mencionadas pertenecen a sus respectivos titulares.

## Referencias técnicas

- [Ficha del NT-2402PRO](https://forza-ups-frontend.s3.amazonaws.com/media/docs/NT-2402PRO_DS_ENG.pdf) y [manual del fabricante](https://forza-ups-frontend.s3.amazonaws.com/media/docs/NT-2402PRO_MANUAL.pdf).
- [NUT: formato QS-Hex](https://github.com/networkupstools/nut/blob/master/drivers/nutdrv_qx_voltronic-qs-hex.c) y [transporte Cypress](https://github.com/networkupstools/nut/blob/master/drivers/nutdrv_qx.c). Se consultaron como referencias de protocolo; el lector y el decodificador Python son implementaciones independientes y no incluyen sus binarios ni código fuente.
- [Microsoft: reportes HID](https://learn.microsoft.com/en-us/windows-hardware/drivers/hid/obtaining-hid-reports) y [HIDP_CAPS](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/hidpi/ns-hidpi-_hidp_caps).
- [Microsoft: shutdown](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/shutdown) y [estados de suspensión](https://learn.microsoft.com/en-us/windows-hardware/drivers/kernel/system-sleeping-states).
