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
- Cualquier endpoint con ese puerto, incluido uno IPv6, dirección comodín o PID desconocido: puerto ocupado; bloquea USB. Una respuesta exacta `monitorOK` identifica Forza, pero una respuesta vacía, distinta, un error o un timeout nunca anulan la ocupación observada.
- Error de API, tamaño o contenido inválido, o una tabla que no se estabiliza dentro del límite: estado desconocido; bloquea USB y conserva el aviso si se agotan los intentos.

Las lecturas de tabla tienen límites de memoria y número de llamadas. La ausencia se comprueba de nuevo en cada lectura, sin caché. El diagnóstico en memoria conserva el resultado actual y hasta 16 cambios o verificaciones que necesitaron reintentos: incluye motivos, familia IP, códigos acotados, duración y resultados de los intentos, sin guardar datagramas ni rutas de procesos. Se expone en `health.usb_ownership`, también en los informes de diagnóstico, y se reinicia con el servicio. No se escribe al disco desde el hilo de adquisición. Las indisponibilidades que se convierten en eventos siguen almacenándose en el historial.

La consulta informa de los endpoints accesibles a la aplicación en el momento de leerla; no prueba por sí sola la propiedad exclusiva del dispositivo USB. Un programa puede comenzar a abrir el UPS antes de publicar su puerto, o entre la consulta y QS. Forza no comparte los mutex de Elipro y el transporte HID admite compartición. Por eso sigue siendo necesario mantener detenido el lector del fabricante y deshabilitado su inicio automático. No se afirma exclusión atómica frente a un Forza que el usuario inicie manualmente.

Las desconexiones USB reales, fallos del lector, datos obsoletos, cuentas atrás y avisos retenidos conservan su comportamiento. No hay un período general de silencio al arrancar ni se borran eventos anteriores para esconder esta incidencia. El instalador cierra primero el agente de la sesión que lo ejecuta y después detiene el servicio, para que una actualización planificada no genere el aviso local de servicio perdido en esa sesión.

Referencias de Microsoft: [GetExtendedUdpTable](https://learn.microsoft.com/en-us/windows/win32/api/iphlpapi/nf-iphlpapi-getextendedudptable), [filas IPv4 con PID](https://learn.microsoft.com/en-us/windows/win32/api/udpmib/ns-udpmib-mib_udprow_owner_pid) y [filas IPv6 con PID](https://learn.microsoft.com/en-us/windows/win32/api/udpmib/ns-udpmib-mib_udp6row_owner_pid).
