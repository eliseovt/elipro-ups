# Comprobación del lector al arrancar

## Incidencia y causa comprobada

En un arranque de Windows con Elipro 0.8.1 se registró esta secuencia local:

| Hora local | Observación |
| --- | --- |
| 06:04:15 | Primera lectura QS válida del UPS. |
| 06:04:21 | La comprobación preventiva de Forza agotó tres consultas UDP y devolvió `timeout`. No se envió QS en ese intento. |
| 06:04:23 | La siguiente comprobación permitió otra lectura QS válida. |

La tarea de inicio de Forza estaba deshabilitada y su última ejecución era anterior al reinicio. La comprobación posterior no encontró procesos de esa instalación ni el puerto UDP 51222 ocupado. Esos datos posteriores no demuestran cuál era la tabla de puertos en el instante del timeout.

El defecto comprobado es que autorizar una lectura nativa dependía de recibir un error de puerto inaccesible al enviar `VP_LINK`. La falta de respuesta podía bloquear la lectura aunque no hubiese un colector. Los reintentos de 0.8.1 distinguían la incertidumbre de Forza confirmado, pero no eliminaban esa dependencia. No se atribuye el timeout a una causa concreta de Windows que no haya quedado registrada.

El inicio del servicio y su agente son procesos distintos. La primera conexión del UPS ya se registra sin generar una notificación de recuperación: solo se notifica recuperación después de una indisponibilidad observada. En esta incidencia el rechazo preventivo ocurrió después de la primera lectura; por eso produjo una pérdida y una recuperación que el agente recibió más tarde.

## Verificación desde 0.8.2

Antes de cada lectura nativa se consulta `GetExtendedUdpTable`, clase `UDP_TABLE_OWNER_PID`, para IPv4 e IPv6. La interpretación es conservadora:

- Ambas tablas completas, válidas y sin el puerto 51222: ausencia confirmada; permite continuar con QS sin enviar UDP.
- Cualquier endpoint con ese puerto, incluido uno IPv6, dirección comodín o PID desconocido: puerto ocupado; bloquea USB salvo la excepción de Chrome identificada descrita abajo. Una respuesta exacta `monitorOK` identifica Forza. Una respuesta vacía, distinta, un error o un timeout por sí solos nunca anulan la ocupación observada.
- Error de API, tamaño o contenido inválido, o una tabla que no se estabiliza dentro del límite: estado desconocido; bloquea USB y conserva el aviso si se agotan los intentos.

Las lecturas de tabla tienen límites de memoria y número de llamadas. La ausencia se comprueba de nuevo en cada lectura, sin caché. El diagnóstico en memoria conserva el resultado actual y hasta 16 cambios o verificaciones que necesitaron reintentos: incluye motivos, familia IP, códigos acotados, duración y resultados de los intentos, sin guardar datagramas ni rutas de procesos. Se expone en `health.usb_ownership`, también en los informes de diagnóstico, y se reinicia con el servicio. No se escribe al disco desde el hilo de adquisición. Las indisponibilidades que se convierten en eventos siguen almacenándose en el historial.

## Colisión con un puerto de Chrome en 0.9.26

El 25 de octubre de 2026 a las 17:57, hora de Lima, se registró `endpoint_occupied` con PID 11300 y respuesta agotada. El proceso seguía vivo y su creación, a las 16:56, precedía al evento: correspondía al servicio de red de Chrome. El último QS válido fue de las 17:57:31. Ese hallazgo explica el bloqueo preventivo de lecturas; por sí solo no explica la detención posterior del servidor HTTP.

Desde 0.9.26 existe una excepción acotada para esa colisión. Todos los propietarios del puerto deben tener PID positivo, una lista por debajo del límite de truncamiento y una imagen exactamente igual a `Google\Chrome\Application\chrome.exe` dentro de Program Files o Program Files (x86). La comparación usa `QueryFullProcessImageNameW` con un handle de consulta del proceso, que permanece abierto hasta terminar la comprobación; no basta que el archivo se llame `chrome.exe`.

Mientras esos handles siguen abiertos se vuelven a consultar ambas tablas UDP: deben mostrar los mismos propietarios o el puerto ya libre. Un snapshot completo de procesos debe descartar `upsMonitor.exe`, `Forza.exe`, `upsTray.exe`, `java.exe` y `javaw.exe`; incluso un Java ajeno conserva el bloqueo. Finalmente se comprueba que cada propietario original siga vivo. Una lista truncada, propietario nuevo, proceso terminado, imagen desconocida, error de API, snapshot incompleto o fallo de cierre de handles no permite la excepción. Una respuesta positiva de Forza siempre prevalece.

La excepción se publica como `endpoint_browser_only` y conserva los PID y el motivo de la respuesta, sin exponer rutas. No cambia el significado genérico de la tabla UDP ni ignora otros navegadores. Sus pruebas usan exclusivamente procesos y API simulados. Igual que la comprobación original, este procedimiento no impide atómicamente que un usuario inicie otro lector justo después; mantener detenido el lector OEM sigue siendo necesario.

La consulta informa de los endpoints accesibles a la aplicación en el momento de leerla; no prueba por sí sola la propiedad exclusiva del dispositivo USB. Un programa puede comenzar a abrir el UPS antes de publicar su puerto, o entre la consulta y QS. Forza no comparte los mutex de Elipro y el transporte HID admite compartición. Por eso sigue siendo necesario mantener detenido el lector del fabricante y deshabilitado su inicio automático. No se afirma exclusión atómica frente a un Forza que el usuario inicie manualmente.

Las desconexiones USB reales, fallos del lector, datos obsoletos, cuentas atrás y avisos retenidos conservan su comportamiento. No hay un período general de silencio al arrancar ni se borran eventos anteriores para esconder esta incidencia. El instalador cierra primero el agente de la sesión que lo ejecuta y después detiene el servicio, para que una actualización planificada no genere el aviso local de servicio perdido en esa sesión.

Referencias de Microsoft: [GetExtendedUdpTable](https://learn.microsoft.com/en-us/windows/win32/api/iphlpapi/nf-iphlpapi-getextendedudptable), [filas IPv4 con PID](https://learn.microsoft.com/en-us/windows/win32/api/udpmib/ns-udpmib-mib_udprow_owner_pid) y [filas IPv6 con PID](https://learn.microsoft.com/en-us/windows/win32/api/udpmib/ns-udpmib-mib_udp6row_owner_pid).
