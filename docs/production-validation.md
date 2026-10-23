# Pruebas conservadas y avisos pendientes de lectura

## Evidencia física existente

Estos resultados corresponden a la unidad Forza NT-2402PRO observada el **18 de octubre de 2026**. Documentan ensayos ya realizados; instalar una corrección o leer este documento no inicia otra prueba ni autoriza nuevos controles USB.

| Operación | Resultado conservado | Alcance |
| --- | --- | --- |
| `de13ebcf-4428-447b-9493-4802686d3a0c` | `cut_observed_cancel_not_tested` | Corte de salida observado y recuperación con el botón. No se ensayó cancelación en esta operación; QS no publicó la señal de corte pendiente. |
| `d211088e-9e60-4a93-91fa-987e0c960b68` | `cancel_observed_by_user` | Cancelación observada mediante una carga independiente que siguió encendida después del plazo. Observación registrada a las 12:07:28, hora de Lima. |
| `a8b574b3-8bff-4615-80c2-7647c0baced6` | `automatic_return_observed` | Corte con batería, regreso de la electricidad y encendido automático de las salidas observados; recuperación de la sesión de Windows. Observación registrada a las 12:45:02, hora de Lima. Retardo de corte informado por el usuario: aproximadamente 180 segundos. |

Las fuentes son los eventos locales de revisión y los registros físicos conservados. Los procedimientos y sus límites se detallan en [prueba de cancelación](ups-cancellation-test.md), [prueba con batería y regreso de electricidad](ups-battery-test.md) y [control del UPS](ups-power-control.md). El retorno automático al volver la electricidad está aceptado para esta unidad. No se extrapolan estos resultados a otra unidad, firmware, carga o autonomía.

Los JSON originales siguen siendo los registros operativos: conservan identificadores, fechas, observaciones y procedencia. Este Markdown facilita la revisión humana y no los sustituye, recrea ni modifica. No contiene autorizaciones temporales ni tokens del panel.

## Corrección del reinicio en 0.9.15

Windows reinició el 19 de octubre a las 06:10, hora de Lima. La versión anterior comparaba el contador monotónico de ensayos antiguos con el contador del nuevo arranque, incluso cuando el registro estaba en `finished`. Al resultar negativo el tiempo transcurrido, descartaba la operación cargada y publicaba `review_required`, sin identificador ni resultado. Esto activaba los dos avisos y bloqueaba el corte automático del UPS.

La carga de los registros ahora valida la estructura del contador sin exigir que pertenezca al arranque actual. Las pruebas terminadas conservan `finished`, sus identificadores y sus resultados. Las operaciones sin terminar continúan exigiendo revisión; no se repite ninguna orden ni se usa un reloj de otro arranque para habilitar una cancelación. Los trabajadores que pueden enviar controles mantienen sus comprobaciones de propiedad, vigencia y tiempo.

## Avisos hasta «Entendido»

Los incidentes eléctricos se guardan como avisos pendientes. Volver a tener electricidad actualiza su contexto, pero no equivale a haberlos leído. Un corte breve y la recuperación posterior siguen disponibles hasta confirmar **Entendido**. Cerrar o recargar el panel y reiniciar el monitor no reconocen esos avisos.

El acuse identifica solamente los avisos presentados. Si llega otro incidente mientras se confirma uno anterior, el nuevo permanece pendiente. El acuse se guarda antes de confirmar el cierre en el panel. **Entendido** y **Cancelar protección** conservan funciones diferentes: leer un aviso no cancela una cuenta atrás, modifica una política ni envía controles al UPS.

## Verificación de la corrección

Las regresiones automatizadas usan telemetría, relojes, notificador y acciones de energía simulados. Cubren registros cerrados tras reiniciar el reloj, operaciones pendientes sin reproducción de órdenes, conservación de resultados, corte breve seguido de recuperación y acuse explícito. La validación de esta versión no requiere repetir las pruebas físicas anteriores.

Verificación del 19 de octubre de 2026:

- Suite Python completa: **849 pruebas aprobadas**. El grupo final de persistencia y avisos también pasó sus **159 pruebas** después del último endurecimiento de almacenamiento.
- Suite de interfaz: **153 pruebas aprobadas**, incluyendo confirmación en lotes de 200 avisos, errores de guardado y llegada de un incidente nuevo mientras se confirma otro.
- Chromium con HTTP simulado: corte de cinco segundos, recuperación, Escape, recarga, confirmación y segunda recarga. Los avisos permanecieron hasta confirmar y no reaparecieron después. Sin errores JavaScript ni desbordamiento horizontal en escritorio de 1440 × 1000 y móvil de 390 × 844.
- Ejecutable y paquete de Windows: compilación y comprobaciones de recursos sin invocar controles físicos ni acciones de energía.

Los avisos guardados no caducan con la retención de las mediciones. Desactivar los avisos de escritorio silencia ese canal y conserva los pendientes del panel. Si Windows bloquea temporalmente la presentación por bloqueo de sesión o modo silencioso, los avisos siguen guardados; el sistema operativo determina cuándo puede mostrarse una ventana nativa.

## Instalación 0.9.15 (registro histórico)

El 19 de octubre quedó instalada **Elipro 0.9.15**, con monitor conectado, salud `healthy` y un agente de sesión de Windows disponible (`ready: true`, sin error de ventanas ni sonido). Ambas pruebas históricas se publican como `finished`, con sus identificadores y resultados originales. Se comprobaron los SHA-256 de los dos diarios antes y después de la instalación: sus bytes no cambiaron. Se respaldaron la base SQLite cerrada y los archivos del directorio de datos antes de actualizar.

En aquella instalación se conservaron la hibernación habilitada, una espera con batería de **30 segundos**, una cuenta atrás de **30 segundos**, la opción de UPS seleccionada y un retardo de **180 segundos**. La admisión del corte físico del UPS permaneció bloqueada porque la espera previa de 30 segundos quedaba fuera de la configuración admitida. La evidencia física acotada de esta unidad corresponde a inicio inmediato y cuenta atrás de 10 segundos; aquella actualización no amplió esa evidencia ni cambió los tiempos elegidos por el usuario. Windows conservó su protección.

No se provocaron cortes eléctricos, se repitieron ensayos físicos, se solicitaron acciones de Windows ni se enviaron controles al UPS durante esta actualización. La comprobación posterior utilizó las lecturas normales del servicio.

## Formulario único en 0.9.16

El diseño aprobado retira «Espera antes del aviso» y sustituye «Tiempo para cancelar» por **Tiempo antes de hibernar**, de 10 a 120 segundos. La cuenta comienza al detectar batería y el aviso permanece hasta pulsar Entendido. El retardo del UPS se configura por separado, de 1 a 9 minutos, contado desde el envío de su orden al solicitar la hibernación.

Se retiran el campo de política activo `on_battery_seconds`, su lógica de espera y las pruebas específicas de ese comportamiento. Se conservan las verificaciones de lecturas, continuidad, cancelación y controles físicos, además de la evidencia histórica de las pruebas. El tiempo transcurrido observado en batería y los invariantes históricos de los diarios no representan una espera configurable.

La migración de configuraciones antiguas conserva el tiempo total cuando está entre 10 y 120 segundos. Si supera ese rango, guarda el original, deja la política desactivada y solicita revisión antes de volver a habilitarla. Esto evita adelantar silenciosamente la hibernación de una política antigua. La instalación del usuario ya tenía espera cero, cuenta de 30 segundos y retardo UPS de 180 segundos; estos dos tiempos se conservan.

La versión **0.9.16 quedó instalada y verificada el 19 de octubre de 2026**: servicio conectado y sano, agente de Windows operativo, política habilitada con **30 segundos antes de hibernar**, retardo UPS de **180 segundos** y admisión del corte del UPS disponible. El campo retirado ya no aparece en la política publicada. Los dos diarios de pruebas físicas conservaron sus bytes y resultados; se respaldaron los datos antes de actualizar.

Verificación de esta entrega: **859 pruebas de Python y 154 de interfaz aprobadas**. Chromium confirmó la ausencia del campo antiguo, el guardado independiente del retardo UPS, la conservación del tiempo de hibernación al recargar y el diseño adaptable de dos columnas en escritorio y una en móvil, sin errores JavaScript ni desbordamiento horizontal. Las pruebas de migración incluyen fallos de almacenamiento y guardado atómico; ninguna prueba ni actualización envió controles al UPS o solicitó acciones de energía de Windows.

## Protección operativa por porcentaje en 0.9.17

El incidente del 19 de octubre a las **19:12:12, hora de Lima**, registró `ups_poweroff_blocked`: la lectura de batería de **25,18 V** fue inferior al valor histórico de 25,41 V, aunque la carga de 11 %, la ausencia de falla y la ausencia de alarma crítica eran válidas. La PC hibernó y el UPS no recibió la orden. Las muestras y eventos originales se conservaron como evidencia, sin atribuir un fallo al temporizador de un minuto.

Por autorización expresa del usuario, se retiraron los mínimos de tensión y el máximo de carga de aquel ensayo como condiciones de uso. **El voltaje de batería no bloquea**. Se comprueban estado QS, fuente y unidad correctas, lecturas recientes, continuidad, salida activa, carga válida inferior al 100 %, ausencia de falla, prueba activa u otra orden pendiente. La alarma crítica real conserva su explicación específica porque el protocolo admite un corte inmediato al enviar una orden en ese estado.

La nueva protección utiliza el porcentaje reportado o, para esta unidad, la estimación conocida identificada como estimada. El umbral guardado de **20 %** queda visible y editable. Si se alcanza funcionando con batería, la cuenta se abrevia a un máximo de 10 segundos sin prolongar un plazo menor ni cambiar el retardo del UPS. La señal crítica real solicita Windows inmediatamente; no se confunde con el porcentaje estimado ni se altera la telemetría original.

Los bloqueos crean avisos persistentes hasta Entendido, con motivo exacto, comprobaciones, valores, tiempos y fecha. El último bloqueo permanece como historial aunque el UPS vuelva a estar disponible. Reconocer uno no oculta otro incidente nuevo dentro del intervalo de repetición.

La comprobación física de la configuración elegida de **10 segundos y 60 segundos** queda pendiente hasta que el usuario la inicie y confirme el resultado observado. Las pruebas con USB y Windows simulados, incluido el replay de la lectura real de 25,18 V, no se presentan como una nueva prueba física ni como una medición de autonomía.

La versión **0.9.17 quedó instalada el 19 de octubre de 2026**, con servicio conectado y sano, agente de Windows listo y admisión del UPS disponible. Se conservaron **10 segundos antes de hibernar**, **60 segundos de retardo UPS** y **20 % como umbral de batería baja**. El estado publicado identifica el voltaje como `blocking: false` y el porcentaje actual como estimado con método conocido. Los diarios físicos originales mantuvieron sus hashes y se respaldaron los datos antes de actualizar.

Verificación final: **891 pruebas de Python y 163 de interfaz aprobadas**. Chromium verificó siete escenarios de escritorio y móvil, guardado del umbral sin alterar tiempos, tensión cero o ausente informativa, rechazo de métodos de estimación desconocidos para la prioridad, avisos persistentes tras recuperar suministro y último bloqueo histórico visible después de Entendido. No hubo errores JavaScript ni desbordamiento horizontal. La comprobación posterior de instalación solo leyó el servicio; **no se inició hibernación ni corte físico del UPS**.

## Salida desde la bandeja en 0.9.18

El 22 de octubre de 2026 se instaló **0.9.18**, con «Salir de Elipro…» en el menú contextual del ícono. Reutiliza la confirmación existente y solicita el mismo cierre autenticado que el panel; el agente se cierra tras la respuesta afirmativa del monitor. Ante un rechazo conserva el ícono y muestra el motivo. El comando interno `agent --stop` mantiene su cierre limitado al agente para las actualizaciones.

Pasaron 79 pruebas de sesión y servicio, y 84 de menú nativo y servidor, con ejecutores simulados. Después de instalar, se comprobó servicio sano, agente disponible y configuración conservada de 10 segundos, 60 segundos y 20 %. Los diarios históricos conservaron sus hashes. La simplificación de restricciones sigue siendo una propuesta separada; esta versión no cambia el criterio de apagado del UPS.

## Cuatro bloqueos operativos en 0.9.19

La implementación del 22 de octubre de 2026 reduce la lógica operativa a cuatro grupos: **UPS sin comunicación o incompatible**, **Windows no está preparado para hibernar**, **otra operación de energía está pendiente** y **el UPS no puede ejecutar un apagado controlado**. Cada grupo conserva el motivo específico. El último incluye falla, alarma crítica real con riesgo de corte inmediato y salida inactiva/no confirmada. El porcentaje bajo configurado sigue adelantando la protección; no bloquea.

Se elimina el límite operativo de carga inferior al 100 %. Carga y voltaje de batería son informativos y sus alertas se conservan. También se eliminan la obligación de una lectura de CA previa y la ventana de tiempo desde aquella lectura. Iniciar el monitor o recuperar USB mientras el UPS sigue con batería permite comenzar una cuenta nueva con datos válidos, sin esperar a que vuelva la red. La evidencia actual `battery_countdown` versión 2 acredita identidad, cuenta cumplida y lectura reciente. El trabajador repite las comprobaciones con QS inmediato antes de escribir; conserva propiedad de la operación, exclusión, vigencia de envío y prevención de órdenes duplicadas.

Los valores y resultados físicos históricos permanecen intactos. Los diarios anteriores siguen siendo legibles para revisión y no autorizan reenvíos al arrancar. La cobertura automatizada incluye arranque y reconexión en batería, carga al 100 % y superior, voltaje ausente, cuenta incompleta, falla, alarma crítica, prueba o corte pendiente y emisión simulada de `S01R0000` una sola vez. Estas comprobaciones usan USB y Windows simulados.

**La prueba física con 10 segundos antes de hibernar y 60 segundos de retardo del UPS sigue pendiente de observación del usuario.** Esta revisión de lógica no acredita un nuevo corte físico, la finalización de Windows antes de ese corte ni autonomía medida.

### Instalación y comprobación de 0.9.19

El 22 de octubre se instaló 0.9.19. El servicio conectado devolvió salud `healthy`, agente de sesión `ready: true`, operación `idle` y los cuatro grupos sin bloqueo. La consulta de hibernación y permisos se verificó desde el servicio real. Se conservaron protección activada, 10 segundos antes de hibernar, retardo del UPS de 60 segundos y umbral bajo de 20 %. Los dos diarios históricos permanecieron en `finished` y conservaron sus SHA-256 originales. Se respaldaron los datos antes de instalar.

- Suite Python completa: **926 pruebas aprobadas**. Tras los últimos ajustes de categorías y compatibilidad, **105 pruebas dirigidas aprobadas**.
- Interfaz: **164 pruebas aprobadas** y cinco escenarios en Chrome, tanto en escritorio como en móvil: disponible, Windows no preparado, falla del UPS, alarma crítica real e histórico recuperado. Cuatro filas, motivos específicos, avisos hasta «Entendido», sin desbordamientos ni errores JavaScript.
- Ejecutable empaquetado: compilación y comprobaciones no invasivas correctas. SHA-256 del instalador final: `84017d61b1f358937f6cf7a516bd7e4c7e839e3bec3560de89db8e65aa7a9848`.
- Pantalla de la instalación real: cuatro filas disponibles, configuración conservada, histórico cerrado y código servido idéntico al fuente por SHA-256. La revisión usó únicamente consultas GET, sin acciones de energía ni acuses de avisos.
- Evidencia local: `.test-data/production-update-0.9.19/` contiene resultados de pruebas, capturas, informe de instalación, verificación posterior y respaldo.

Durante esta actualización no se solicitó hibernación ni se enviaron comandos de corte o cancelación al UPS. Los ensayos automatizados de la secuencia usan ejecutores simulados; la comprobación física pendiente no se sustituye por esos resultados.

## Pantalla compacta instalada en 0.9.20

El 22 de octubre de 2026 se implementó e instaló la distribución aprobada: **Windows → Batería baja → Apagado del UPS**, con el interruptor maestro en la cabecera y el interruptor del UPS alineado. La secuencia, los tres ajustes, las cuatro condiciones y Guardar configuración caben sin desplazamiento en el estado normal a 1366 × 768, 1600 × 900 y 1920 × 1080. En móvil se apilan sin desbordamiento horizontal. Los motivos activos permanecen visibles; las notas y el bloqueo histórico están en un desplegable.

Se conservaron el comportamiento de protección, sus cuatro bloqueos, los avisos hasta Entendido y la configuración de 10 segundos, 60 segundos y 20 %. El control maestro mantiene su asociación con el formulario, guarda correctamente los cambios y conserva el borrador al actualizar las lecturas o navegar.

- **165 pruebas de interfaz aprobadas**, incluyendo el interruptor externo al formulario, cambios sin guardar, envío de los mismos ajustes y estados de bloqueo.
- **16 escenarios visuales aprobados**: escritorio, móvil, modo claro, desconexión, falla, alarma crítica, detalles y acción de apagado. No se recortan motivos para forzar una altura fija.
- Verificación de la instalación mediante consultas GET: versión 0.9.20, servicio `healthy`, conexión disponible, agente `ready: true` y cuatro condiciones disponibles. Los SHA-256 del HTML, JavaScript y CSS servidos coinciden con las fuentes.
- Configuración y hashes de los diarios históricos conservados; datos respaldados antes de actualizar. No se solicitaron acciones de energía durante esta entrega.
- Instalador final SHA-256: `1c2f1a650adbb240f49e2c22039ea9fefc2f293fbeae1ba27b90842d170bb02c`.

Los resultados y capturas están en `.test-data/production-update-0.9.20/`, incluidos `installed-compact-browser-qa.json` e `installed-compact-1600x900.png`.
