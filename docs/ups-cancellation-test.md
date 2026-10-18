# Prueba física manual de cancelación del UPS

Documento histórico de la validación inicial y referencia técnica de diagnóstico. Desde Elipro 0.9.12 estas tarjetas y sus controles no forman parte de la pantalla normal de Protección. Los estados pendientes descritos a continuación corresponden a cada ensayo y no representan por sí solos el estado actual de la protección instalada. Los resultados se conservan para trazabilidad.

La prueba real es independiente de «Simular cuenta atrás» y de «Apagar también el UPS». No crea perfiles físicamente validados. Una cancelación no validada puede fallar y cortar la salida que alimenta esta PC, incluso antes del retardo esperado. Guardar documentos reduce la exposición, pero no aísla eléctricamente el equipo.

## Observación física del 18 de octubre de 2026

En el primer ensayo real, operación con prefijo `de13`, Elipro envió una única orden `S03R0000\r`. **No se envió `C\r` y no se probó la cancelación.** Las horas siguientes corresponden a Lima (UTC−05:00):

| Hecho | Hora y alcance de la evidencia |
| --- | --- |
| Inicio de la orden de corte a 180 segundos | 11:04:19, según el registro de la operación. |
| Lectura durante la espera | A las 11:06:04 seguía reportando `Line mode`, flags QS `09`, salida de 211,8 V y batería baja falsa. El bit de apagado pendiente no apareció. |
| Notificación de suspensión de Windows | 11:07:05.880. Esta notificación no mide el instante en que terminó de escribirse la imagen de hibernación. |
| Plazo previsto de la orden | Aproximadamente 11:07:19, calculado a partir del inicio y de los 180 segundos solicitados. **No es una medición de la hora del corte físico.** |
| Corte de salida | Confirmado por el usuario: la PC hibernó y el UPS cortó la salida. No se cronometró el instante exacto del corte. |
| Restauración de la salida | El usuario confirmó que necesitó pulsar el botón del UPS. El indicador azul había permanecido encendido aunque no había tensión de salida; ese indicador por sí solo no acredita salida activa. |
| Reanudación de Windows | 11:08:44, después de la restauración manual de la salida. |

Este ensayo aporta evidencia de que **esta unidad** ejecutó la orden de corte y permitió restaurar la salida mediante su botón, aunque QS no mostró el bit pendiente. No demuestra cancelación, comportamiento con batería baja, reserva de batería ni funcionamiento equivalente en otra unidad o firmware. Las dos pruebas anteriores de hibernación de Windows siguen siendo dos pruebas separadas; este ensayo no cambia sus mediciones.

El cierre de esta operación debe conservar los hechos `output_cut_observed`, `pending_signal_not_reported` y `manual_start_observed: true`, con resultado `cut_observed_cancel_not_tested`. No debe registrarse como cancelación correcta ni crear un perfil de producción. La repetición con compatibilidad para esta unidad requiere una nueva autorización temporal y la versión que incorpore las comprobaciones siguientes; todavía no es evidencia de una segunda prueba realizada.

## Preparación y autorización

La autorización se genera desde el ejecutable **instalado**, con permisos de administrador:

```powershell
& 'C:\Program Files\Elipro UPS\Elipro.exe' authorize-ups-cancel-test --minutes 15
```

Este comando no consulta ni controla el hardware, no habilita el apagado automático del UPS y no cambia la política de Windows. Crea una autorización de un solo uso que vence como máximo en quince minutos, dentro del directorio del servicio. Exige el servicio propio activo, lectura nativa y permiso de acciones Windows; los trabajadores de prueba además verifican LocalSystem real. No acepta rutas arbitrarias, comandos, retardos o perfiles aportados por el cliente.

## Secuencia del usuario

1. Mantener conectada la electricidad de casa y encendido el UPS. No usar el interruptor de corte durante este ensayo.
2. Guardar los documentos y cerrar operaciones importantes de escritura.
3. Desactivar y guardar temporalmente la protección automática de Windows. «Apagar también el UPS» permanece desactivado.
4. En Protección, abrir «Prueba real de cancelación del UPS», leer la advertencia y confirmar que se guardó el trabajo. Solo el botón final envía la orden real fija de corte a 180 segundos.
5. Pulsar «Cancelar prueba del UPS» en cuanto se habilite; no esperar al vencimiento. Para una unidad sin observación física previa se exige el bit pendiente QS. La excepción de prueba para la misma unidad del primer ensayo se describe más abajo: un QS `09` no permite esperar una confirmación que esta unidad no reportó.
6. La opción recomendada con una carga independiente ya conectada (por ejemplo, el router de esta instalación) es hibernar manualmente la PC poco después del envío de `C`, sin forzar aplicaciones, y observar el **indicador de alimentación del router** hasta treinta segundos después del plazo original. No basta comprobar Internet o la pantalla de la PC. El router debe estar en una salida controlada y respaldada por batería; no se cambia la conexión eléctrica de la PC. Así Windows ya está hibernado si la cancelación falla. QS `09` después de `C` no prueba éxito si antes también era `09`.
7. Al reanudar, el registro queda en revisión y nunca repite órdenes. El panel permite indicar si la carga independiente siguió encendida o se cortó, siempre después del plazo original más treinta segundos. Ambos resultados conservan su procedencia como observación del usuario; solo guardan metadatos. Después se puede volver a activar la protección de hibernación. El perfil automático del UPS continúa bloqueado.

## Contraste con documentación pública

- La [guía de ForzaTracker](https://forza-ups-frontend.s3.amazonaws.com/media/docs/windows_installation_guide.pdf#page=34) separa el cierre de Windows y el corte posterior del UPS. Es una descripción funcional genérica: no especifica una confirmación de finalización S4/S5 para este modelo.
- La [tabla QS-Hex de NUT](https://raw.githubusercontent.com/networkupstools/nut/master/drivers/nutdrv_qx_voltronic-qs-hex.c) usa `C\r` tanto para cancelar como para encender salidas, y conserva los retardos como valores del programa. Su [ejecución de comandos](https://raw.githubusercontent.com/networkupstools/nut/master/drivers/nutdrv_qx.c) no exige ese bit pendiente antes de enviar la orden. El contador de Elipro se etiqueta como tiempo calculado desde el envío, no como contador leído del hardware.
- El [protocolo QS](https://networkupstools.org/protocols/voltronic-qs.html) permite controles aceptados sin respuesta y documenta corte anticipado por batería baja. Ni silencio ni escritura HID satisfactoria acreditan ejecución o cancelación.
- Las [limitaciones QS-Hex de NUT](https://networkupstools.org/docs/man/nutdrv_qx.html#_known_problems) incluyen unidades que vuelven a alimentar al recuperar la red pese a pedir permanencia apagada. No se extrapola ese comportamiento a este NT-2402PRO ni se da por descartado por una prueba que mantuvo la red presente.

La falta de señal de pendiente en esta unidad procede de la observación local, no de una afirmación del fabricante sobre todo el modelo. El primer corte se corroboró por el router y el monitor apagados después de que Windows entrara en hibernación, según el usuario. La restauración con el botón se observó con red presente; sigue pendiente comprobar el regreso de la red tras un corte real de entrada.

Si no se puede corroborar o cancelar, se muestra resultado incierto y el plazo original continúa visible. No repetir la orden ni reiniciar el servicio o Windows mientras pueda seguir pendiente. La contingencia de hibernación normal debe presentarse con antelación suficiente al corte, sin esperar a que el contador llegue a cero; después se revisa el estado físico antes de volver a usarlo. Este ensayo no completa la validación necesaria para el apagado automático del UPS.

## Límites de implementación

- Una operación durable por autorización, escrita antes del primer reporte HID; ningún reintento automático.
- Comandos cerrados `S03R0000\r` y `C\r`, sin reinicio automático, reparto entre reportes de ocho bytes útiles y mutex USB completo.
- Inicio solo con QS reciente, salida activa, red presente, sin batería baja, falla u otra operación pendiente. El trabajador vuelve a comprobar la evidencia inicial y la unidad.
- Cancelación de la misma unidad y operación, una sola vez, con bit pendiente corroborado después del envío, salida activa y más de diez segundos de margen.
- Excepción exclusiva de prueba para la unidad del ensayo documentado: el bit pendiente puede faltar solo si una operación previa cerrada de esa misma huella conserva la observación física de corte sin bit QS. Se exige un envío propio completo de `S03R0000\r`, consulta QS inmediata con red y salida activas, sin batería baja ni falla, plazo monotónico menor de 170 segundos y ningún intento anterior de `C`. El trabajador consulta QS directamente dentro del mismo mutex USB antes de decidir; no inicia un lector hijo recursivo. Una suspensión, reanudación o reinicio impide usar esta excepción para cancelar.
- Plazos físicos basados en reloj monotónico compartido por los procesos; UTC se conserva como evidencia y para la autorización temporal. Un proceso anterior o un reinicio exige revisión, nunca reproducción.
- API con origen/CSRF y confirmación estricta. Lecturas, simulación, arranque del servicio y reanudación no pueden iniciar la prueba.
- Las acciones automáticas, la simulación y el cierre desde el panel quedan bloqueados mientras la prueba esté pendiente o incierta.
- La excepción anterior no se aplica al apagado automático del UPS, no modifica el registro de perfiles de producción y no permite certificar cancelación únicamente por la ausencia del bit QS.
