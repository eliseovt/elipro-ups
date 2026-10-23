# Prueba manual con batería y regreso de electricidad

Referencia histórica del procedimiento usado durante la validación inicial. Desde Elipro 0.9.12 los formularios de este ensayo se retiraron de la interfaz normal de Protección; sus observaciones y la lógica de diagnóstico permanecen conservadas. Los pasos y estados que siguen describen aquel procedimiento, no tareas pendientes de uso diario.

Este ensayo comprueba si la unidad observada corta sus salidas al funcionar con batería y cómo restablece la alimentación cuando regresa la electricidad. Solicita hibernar Windows sin forzar aplicaciones. Es independiente de la protección automática y de la prueba de cancelación; registrar un resultado favorable no habilita el apagado automático del UPS.

El usuario acepta que las salidas vuelvan a encenderse automáticamente al regresar la electricidad. Ese comportamiento ya observado se conserva como evidencia física válida de esta unidad; no es una incompatibilidad ni exige repetir la prueba de retorno de entrada. Tampoco se exige que permanezca apagada sesenta segundos como condición de la modalidad elegida. Esta observación no acredita por sí sola otras condiciones de ejecución.

La PC conserva su conexión eléctrica actual. El router conectado al UPS sirve como carga independiente: se observa su **indicador de alimentación**, no la disponibilidad de Internet. La luz azul del UPS o una pantalla negra no prueban por sí solas que sus salidas estén sin tensión.

## Secuencia de hibernación comprobada

La evidencia física existente se utiliza para una secuencia acotada: inicio inmediato al detectar batería, cuenta atrás cancelable de **10 segundos**, solicitud de hibernación sin forzar aplicaciones y corte del UPS a los **180 segundos desde el envío**. El encendido de las salidas al regresar la electricidad está aceptado. La pantalla la denomina «Secuencia de hibernación comprobada»; no es una medición de autonomía ni una reserva garantizada para otras esperas, cargas o estados de batería.

La configuración operativa actual permite elegir un único **Tiempo antes de hibernar**, entre **10 y 120 segundos**, y un retardo independiente del UPS de **1 a 9 minutos**. El aviso aparece inmediatamente al detectar el corte; no hay una espera previa configurable. El porcentaje bajo puede abreviar la cuenta a un máximo de 10 segundos, con fuente reportada o estimada explícita. Desde 0.9.19 ni el voltaje de batería ni la carga del UPS bloquean por sí solos. La producción usa cuatro grupos visibles: comunicación/compatibilidad, Windows preparado, operación pendiente y posibilidad de apagado controlado. Iniciar o recuperar USB en batería permite una nueva cuenta válida sin exigir CA previa ni la ventana temporal del ensayo. Una acción distinta de hibernar conserva únicamente la protección de Windows. Los rangos configurables no acreditan ensayos físicos nuevos: la evidencia histórica sigue correspondiendo a la cuenta de 10 segundos y el retardo de 180 segundos observados. El protocolo de ensayo manual descrito a continuación conserva sus condiciones propias.

La disponibilidad para configurar el interruptor se distingue de la admisión de una ejecución concreta. El servicio vuelve a comprobar lectura, unidad y condiciones al vencer la cuenta atrás. La simulación respeta el tiempo guardado; representa la espera de 180 segundos del UPS solo para la configuración admitida y excluye esa etapa para las otras configuraciones, sin escribir controles ni solicitar acciones reales a Windows.

No se solicita repetir las observaciones físicas de retorno de entrada ya registradas para describir esta secuencia. Los pasos de ensayo siguientes documentan el procedimiento independiente y conservan su confirmación manual; no se inician al cambiar la política.

## Antes de interrumpir la entrada

1. Leer toda la secuencia con la electricidad de casa presente. Guardar los documentos y terminar copias, actualizaciones u otras escrituras importantes. Comenzar con el UPS cargado y sin alertas.
2. Desactivar y guardar «Protección automática». Mantener desactivado «Apagar también el UPS». El ensayo usa su propia solicitud de hibernación y no cambia la acción guardada.
3. Conservar PC, monitor y router en sus salidas actuales del UPS. Usar solamente el interruptor de entrada ya conocido para simular el corte. No desenchufar salidas ni cambiar la conexión eléctrica de la PC.
4. Comprobar que la prueba de cancelación de **esta misma unidad** quedó cerrada con observación física favorable de una carga independiente. Un registro con resultado incierto, otra huella USB o solo una escritura de `C` no sirve como ese antecedente.
5. Obtener la autorización administrativa temporal desde el ejecutable instalado:

   ```powershell
   & 'C:\Program Files\Elipro UPS\Elipro.exe' authorize-ups-battery-test --minutes 15
   ```

   Este comando solo prepara una autorización de un uso, de hasta quince minutos. No escribe controles USB, no inicia hibernación, no cambia la política y no crea un perfil de producción validado.

El modo de prueba exige lecturas recientes, una transición a batería recién observada, salida activa, carga de hasta el 40 %, ausencia de batería baja y falla, y ninguna operación pendiente. La ventana para iniciar es de treinta segundos desde la transición observada; se debe leer y preparar todo antes de accionar el interruptor. Si una condición falla, el botón permanece bloqueado con su motivo. No se debe repetir un corte de entrada sin revisar primero ese motivo.

## Ejecución desde Protección

1. Abrir la tarjeta «Comprobar el corte con batería y el regreso de electricidad» y preparar un cronómetro independiente.
2. Interrumpir la electricidad **de entrada** con el interruptor conocido. Esperar a que Elipro indique funcionamiento con batería. Este cambio por sí solo **no** inicia la prueba ni envía órdenes.
3. Pulsar «Preparar prueba con batería». Leer la advertencia, confirmar que se guardó el trabajo y pulsar el botón final «Sí, probar corte e hibernar».
4. El servicio registra la operación, envía una única orden fija de corte retardado `S03R0000` y solicita inmediatamente hibernar Windows, sin `/f`. El retardo solicitado es de tres minutos. El contador de Elipro se calcula desde el envío; no se lee del UPS y no mide el instante físico del corte. Si Windows sigue activo treinta segundos después del inicio, usar Windows + R y ejecutar `shutdown.exe /h`, sin forzar aplicaciones.
5. Observar que el gabinete termine de apagarse antes de que se apague el indicador de alimentación del router. La solicitud aceptada por Windows no confirma que la escritura de hibernación haya terminado. No usar el interruptor de salida ni el botón del UPS para provocar el resultado esperado.
6. **Solo después del corte de las salidas**, restablecer la electricidad de entrada con el interruptor y observar el indicador de alimentación del router. Anotar si la alimentación vuelve sola y cuánto tiempo permaneció apagada; registrar cero segundos si vuelve inmediatamente. El retorno automático está aceptado. No se solicita repetir una observación ya registrada ni demostrar permanencia apagada durante sesenta segundos.
7. Si las salidas vuelven solas, encender la PC para reanudar. Si permanecen apagadas, restablecerlas con el botón del UPS y después encender la PC. Comprobar que Windows recupere la sesión y las aplicaciones.
8. Con lecturas nuevas, electricidad presente y el plazo original más treinta segundos ya transcurrido, registrar la observación en la misma tarjeta.

Si no se observa el corte al vencer los tres minutos, esperar hasta tres minutos y treinta segundos desde el envío. Después restablecer la entrada, manteniendo la PC hibernada. **Restablecer la electricidad no anula una orden programada.** No enviar una cancelación genérica ni prolongar el ensayo hasta agotar la batería. El resultado pendiente exige revisión; no se interpreta como prueba favorable. Tampoco se provoca una alerta de batería baja para probar el corte anticipado.

Si se agotan los treinta segundos iniciales sin haber pulsado el botón final de autorización, restablecer la entrada y revisar el motivo del bloqueo. En ese caso esta prueba no envió ninguna orden ni programó un corte; no es necesario repetir inmediatamente el cambio de entrada.

Si Windows rechaza la hibernación, el servicio puede intentar cancelar **una sola vez** su propia orden dentro del margen y las condiciones comprobadas. Una solicitud de cancelación no prueba que el hardware la haya aceptado. El aviso y el diario conservan esa incertidumbre; no se repiten controles ni se presenta el corte como anulado.

## Registro físico y resultados

El formulario pide respuestas explícitas sobre corte, restauración de entrada, encendido automático observado, duración sin salida, necesidad del botón manual y recuperación de Windows. La confirmación guarda únicamente metadatos vinculados a la operación y a la unidad; no escribe en el hardware.

| Resultado | Interpretación |
| --- | --- |
| `battery_cut_manual_restart_observed` | Registro histórico de corte, regreso de entrada, al menos sesenta segundos sin salida, encendido manual y recuperación de la hibernación, sin retorno automático observado. No es una condición que deba cumplir la modalidad ahora elegida ni una aprobación del perfil. |
| `automatic_return_observed` | El usuario observó expresamente que las salidas se encendieron solas al regresar la entrada después del corte. Es el comportamiento aceptado para esta unidad; otras condiciones de habilitación siguen pendientes. |
| `physical_test_incomplete` | Falta alguna comprobación necesaria. No se infiere un comportamiento que el usuario no pudo observar ni se aprueba el perfil. |

Un intervalo menor de sesenta segundos no demuestra por sí solo retorno automático: el usuario podría haber pulsado antes el botón. Se necesita la respuesta explícita `automatic_return_observed`. Si no pudo comprobar algún punto, debe utilizar «No pude comprobar: registrar incompleta»; se conservan las comprobaciones como no confirmadas.

El estado público conserva esa observación sin modificarla y añade, solo para la misma unidad, `restart_behavior: automatic_on_mains`, `restart_behavior_observed: true`, `automatic_restart_accepted: true` y `manual_restart: false`. Estos campos describen el comportamiento aceptado; no equivalen a `validation: validated` ni habilitan por sí solos los controles automáticos.

La API de observación usa `operation_id`, `cut_observed`, `ac_restored`, `off_after_ac_seconds`, `manual_restart`, `hibernate_resumed`, `automatic_return_observed` y `acknowledge_observation`. Solo el botón final del usuario inicia la prueba. Las lecturas, los sondeos del panel, la suspensión, la reanudación y el arranque del servicio nunca la repiten ni levantan el bloqueo de producción.

## Alcance

El [protocolo QS](https://networkupstools.org/protocols/voltronic-qs.html) permite órdenes aceptadas sin respuesta y contempla un corte anticipado por batería baja. Las [limitaciones QS-Hex](https://networkupstools.org/docs/man/nutdrv_qx.html#_known_problems) incluyen equipos que restituyen sus salidas al volver la entrada aun cuando se solicite permanencia apagada; el ensayo verifica esta unidad sin extrapolar a todo el modelo.

Según el [orden documentado de hibernación de Windows](https://learn.microsoft.com/en-us/windows/win32/power/system-power-states#entering-hibernation), las notificaciones de suspensión preceden al guardado completo de memoria. Por tanto, ni el retorno de la solicitud ni una notificación permiten afirmar que ya terminó. Los tres minutos solicitados son un margen, no una garantía ante cualquier bloqueo de Windows.

Este documento describe el procedimiento y sus límites. No declara que se haya ejecutado ni que exista un nuevo resultado físico favorable. La promoción del perfil automático y la validación por acción siguen separadas del ensayo.
