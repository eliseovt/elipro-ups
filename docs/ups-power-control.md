# Control de salida del NT-2402PRO

## Estado de esta entrega

La comunicación de estado de la unidad observada usa USB `0665:5161`, revisión USB `0003`, cadenas genéricas `INNO TECH` / `USB to Serial` y reportes `9/9/0`. QS/T permite confirmar comunicación, no identidad comercial o firmware únicos. La revisión USB no es una versión de firmware.

El registro `VALIDATED_PROFILES` contiene un único perfil de **secuencia de hibernación observada**, ligado a la huella `7df095c878742cf146a631909fd5f39946926b787720d9932263d2805e59399b`. No aprueba otras unidades, apagado de Windows ni una autonomía genérica de batería. El perfil no habilita por sí mismo permisos administrativos o la política: esos controles siguen siendo independientes. No hay una API para importar perfiles, mandar bytes arbitrarios, cortar inmediatamente o programar reinicios. El retorno automático de las salidas al restablecer CA es aceptado; no requiere una orden nueva de reinicio.

El perfil `nt2402pro-qs-t-rev3` separa la configuración operativa elegida por el usuario de la evidencia histórica. La configuración actual es:

| Ajuste o condición operativa | Valor o límite |
| --- | --- |
| Acción de Windows | Hibernar sin forzar aplicaciones |
| Espera previa en batería | 0 segundos |
| Tiempo para cancelar | De 10 a 120 segundos; predeterminado: 15 segundos |
| Tiempo para apagar el UPS | De 1 a 9 minutos enteros; predeterminado: 3 minutos |
| Carga observada, incluida la lectura inmediata del trabajador | Como máximo 11 % |
| Voltaje de batería en la lectura previa de CA | Al menos 27,41 V |
| Voltaje de batería durante la secuencia | Al menos 25,41 V |
| Orden UPS | `S01R0000\r` a `S09R0000\r`, según el tiempo elegido, una sola vez |
| Retorno de salida esperado | Automático al regresar CA |

El tiempo del UPS comienza cuando se envía su orden, al iniciar la solicitud de hibernación; no comienza al detectar el corte ni espera confirmación de que Windows haya terminado. Con los valores predeterminados se muestran quince segundos para cancelar; después se programa el UPS a tres minutos y se solicita hibernar la PC. Los tiempos son independientes.

Todos los ajustes y las notas de funcionamiento permanecen visibles. Hay un único botón **Cancelar protección** durante el aviso inicial: detiene esta actuación de Windows y del UPS hasta el próximo corte. Después de programar el UPS no aparece otra cuenta cancelable. El temporizador posterior es informativo; no se ofrece una cancelación genérica del UPS al usuario.

La evidencia histórica se mantiene sin modificar:

| Dato del ensayo registrado | Observación |
| --- | --- |
| Cuenta atrás de la configuración observada anteriormente | 10 segundos |
| Ventana conservadora que usaba esa configuración | 15 segundos desde la última CA |
| Tiempo activo de batería registrado antes de iniciar el ensayo | 12,656 segundos |
| Tiempo desde la última CA hasta el inicio registrado | 15,016 segundos |
| Tiempo desde la última CA hasta completar el envío | 15,353 segundos |
| Retardo UPS solicitado y observado | 180 segundos solicitados; el usuario informó aproximadamente tres minutos |

Cambiar la configuración a quince segundos o elegir otro retardo no cambia esos datos ni acredita una prueba física nueva. Los voltajes delimitan condiciones de operación; no se convierten en porcentaje de carga o autonomía. `action_validation.hibernate.sequence_validation` conserva el ensayo original y `operation_configuration` permite explícitamente la configuración de usuario: cuenta de hasta 120 segundos, retardo de 60 a 540 segundos, espera previa de cero y margen de sondeo/envío de diez segundos después de la cuenta. Ese margen es un límite de continuidad operativa, no una reserva de batería medida.

El identificador `reserve_mode: observed_sequence` se conserva para este perfil, sin `reserve_seconds` ni un supuesto tiempo de recarga. El valor predeterminado de quince segundos no se rechaza por confundirlo con el límite histórico de diez. Permanecen las comprobaciones de unidad, fuente nativa, lectura fresca, carga, voltaje y continuidad; huecos, reinicio, suspensión, pérdida de identidad, batería baja o falla impiden armar el corte. Si no se admite el control del UPS antes de enviarlo, se conserva el camino de protección de Windows cuando sus propias condiciones permiten actuar.

El retardo operativo debe superar el mayor tiempo de hibernación observado en al menos 45 segundos: con el máximo informado de 15 segundos, el mínimo configurable es un minuto. Es un margen operativo elegido, no una medición nueva de autonomía ni una garantía ante bloqueos de Windows.

### Contrato de política y estado

`policy.countdown_seconds` es un entero de 10 a 120, predeterminado 15. `policy.ups_poweroff_delay_seconds` es un entero de 60 a 540 y múltiplo de 60, predeterminado 180. Este segundo campo guarda segundos aunque el formulario muestre minutos. No se admite ni se redondea silenciosamente un valor de diez minutos: la documentación específica QS-Hex limita este comando a nueve. La actualización conserva ajustes guardados; la activación explícita puede establecer los nuevos valores acordados.

Las capacidades distinguen:

- `delay_seconds`: tiempo seleccionado para la próxima orden.
- `observed_delay_seconds`: los 180 segundos de la observación histórica.
- `delay_protocol_supported`: si el valor elegido está en los minutos enteros documentados, de uno a nueve.
- `delay_hardware_validated`: verdadero solo cuando el retardo elegido coincide con los 180 segundos físicamente observados. No certifica una nueva combinación de cuenta atrás, carga o autonomía.

El servidor transmite el retardo seleccionado al coordinador. El diario conserva ese valor en `delay_seconds` y la evidencia continua de la operación antes de escribir USB; el trabajador vuelve a comprobar ambos. Los plazos de cancelación de emergencia y revisión se calculan con el valor realmente enviado, sin reutilizar tres minutos fijos cuando se eligió otro tiempo. La simulación también usa los dos tiempos guardados y nunca envía controles.

## Evidencia del protocolo

El [manual del modelo](https://forza-ups-frontend.s3.amazonaws.com/media/docs/NT-2402PRO_MANUAL.pdf) documenta programación desde software. El programa original distingue cerrar Windows y programar el UPS. Su implementación descarta respuestas de control y produce un ACK sintético; no lo usamos como prueba de ejecución.

La [especificación QS](https://networkupstools.org/protocols/voltronic-qs.html) describe `S<n>R0000` como solicitud de salida apagada, y `C` para cancelar un corte pendiente. Los controles válidos pueden no responder. Con batería baja el corte puede adelantarse. [QS-Hex documenta diferencias de retorno al recuperar CA](https://networkupstools.org/docs/man/nutdrv_qx.html#_known_problems); el comportamiento real se registra por unidad. El retorno automático observado y aceptado no se clasifica como fallo por no exigir el botón manual.

La implementación limita el codificador a minutos enteros de 1 a 9 y prohíbe apagado inmediato, retardos fraccionarios, reinicio programado y concatenación de comandos. El mínimo calculado de 180 segundos se conserva en la evidencia histórica y en la modalidad de reserva medida; no se presenta como una medición física de todos los tiempos configurables. La orden ocupa nueve bytes y se divide en dos reportes de ID cero y ocho bytes útiles. El retorno de carro queda al final de la orden. Se mantiene el mutex y el plazo sobre la transacción entera; no se reenvía tras un resultado parcial o incierto.

## Evidencia y modalidades de validación

La aprobación es específica de unidad/conexión USB y de cada acción, apagado o hibernación. Un cambio de hardware o conexión requiere revisar la evidencia. No se habilita control para toda una familia por compartir descriptor.

La secuencia registrada utiliza estas observaciones de la misma unidad:

- `de13ebcf-4428-447b-9493-4802686d3a0c`: corte real sin bit QS pendiente reportado.
- `d211088e-9e60-4a93-91fa-987e0c960b68`: cancelación enviada y observación independiente sin corte posterior al plazo original.
- `a8b574b3-8bff-4615-80c2-7647c0baced6`: secuencia en batería, hibernación recuperada, corte aproximado a tres minutos y retorno automático al volver CA.
- Dos ensayos normales de hibernación informados entre diez y quince segundos; se conservan como cantidad y rango, sin inventar una tercera medición.

El comportamiento de batería baja se trata como **supuesto conservador del protocolo**: el corte podría adelantarse inmediatamente. No se afirma haber ensayado descarga profunda o batería baja. Si ya existe la señal baja, no se arma el UPS y se conserva la protección de Windows.

La modalidad alternativa `measured_reserve` continúa requiriendo evidencia de:

- Al menos dos ensayos completos de Windows por acción y reserva medida que cubra el retardo resultante: `max(180, ceil((max(tiempos) + 120) / 60) * 60)`. Un rango informado se conserva como rango y cantidad de ensayos; no se inventan mediciones individuales. Los dos ensayos de hibernación informados, entre 10 y 15 segundos, corresponden a un retardo mínimo de 180 segundos.
- Temporización física, cancelación pendiente y comportamiento ante batería baja.
- Comportamiento observado al volver CA. Se admite `restart_behavior: automatic_on_mains` con `restart_behavior_verified: true`, ligado a la huella validada. Los perfiles antiguos con `manual_restart_verified: true` conservan su significado de encendido manual (`manual_only`); este comportamiento no es un requisito para la unidad del usuario.
- Confirmación independiente de los resultados físicos; la ausencia de respuesta o el retorno de una API no son esa confirmación.

La instalación actual no permite aislar la única PC. Los ensayos físicos se realizaron con consentimiento explícito y sus límites se conservan; no equivalen a aislamiento eléctrico ni a autonomía medida bajo carga normal durante todo el temporizador. Las pruebas de software y el diagnóstico de lectura no aprueban otras unidades o secuencias.

## Secuencia y fallos

La cuenta atrás cancelable sucede antes de cualquier escritura de control. Volver CA, perder lecturas válidas o pulsar Cancelar durante ese aviso cancela la actuación pendiente. Al vencer, el servicio verifica fuente nativa, ambos permisos administrativos, perfil, unidad, lectura reciente y batería sin señal baja. El perfil actual exige evidencia continua dentro de la configuración operativa; un perfil de reserva medida exigiría su reserva restante. Después guarda un registro durable, solicita una sola vez el corte retardado y pide inmediatamente cerrar Windows. La política no cambia las configuraciones de hibernación ni fuerza el cierre de aplicaciones. Regresar CA después de enviar la orden no equivale a anularla.

El diario distingue envío completo, resultado incierto y estado de apagado pendiente corroborado por una lectura QS posterior al envío. Ninguno significa que Windows haya terminado. Si Windows rechaza el cierre, solo se intenta cancelar una operación propia con estado pendiente reciente y salida todavía activa; `C` puede reencender una salida y no se utiliza como recuperación genérica.

Cada cancelación se intenta una sola vez y exige un envío propio completo. La mera ausencia de un bit que nunca apareció no confirma cancelación. Un perfil que documente esa señal defectuosa solo puede cancelar su operación propia, con lectura inmediata, salida activa y margen suficiente; requiere observación continua hasta el plazo original más treinta segundos para cerrar una cancelación sin señal. Un diario corrupto o pendiente al arrancar requiere revisión: no se repiten órdenes al reiniciar el servicio ni al reanudar de hibernación. Un fallo de esta capacidad no debe detener las lecturas ni impedir el apagado de Windows si el lector sigue sano.

La excepción de señal pendiente se documenta en `pending_signal_evidence`, con la misma huella y observaciones de corte, ausencia de señal y cancelación sin corte. Para un perfil de retorno automático se exige `automatic_return_observed: true`, sin pedir `manual_start_observed`; los perfiles manuales antiguos conservan esta última evidencia. Las capacidades y los diarios distinguen `restart_behavior` y `manual_restart`: este último es falso cuando el retorno esperado es automático. Un diario antiguo sin el campo nuevo solo se acepta con su condición manual original. La orden sigue siendo `S03R0000\r` cuando el retardo validado es de tres minutos; no se añade un comando de reinicio.

Los controles usan un evento nativo por operación, conservado por el proceso propietario y señalado inmediatamente cuando Windows anuncia suspensión. El trabajador lo comprueba antes y después de consultar QS y antes de cada fragmento de control. También vuelve a comprobar la continuidad y la ventana operativa antes de completar la orden. Un evento ausente o señalado bloquea la escritura; no se completa una orden pausada al reanudar. Después de un posible envío, la solicitud de Windows se ejecuta antes de actualizar el diario o leer el estado. El hecho de envío completo queda en memoria antes del callback y no se pierde si este inicia la hibernación.

La reserva medida, cuando un perfil la documente, se sigue independientemente del temporizador de la política. Sin autonomía real del hardware, esa modalidad requiere una condición de recarga (`reserve_baseline.recharge_seconds`, `min_battery_voltage`) y un límite de carga validados. Una vuelta breve de electricidad no repone la reserva completa. Arranque, suspensión, huecos, pérdida de identidad y carga desconocida o excesiva invalidan la reserva medida. El lector QS actual no informa autonomía real y su porcentaje estimado nunca concede reserva. La configuración operativa del perfil actual no inventa esa autonomía ni una condición de recarga.

El [orden de hibernación de Windows](https://learn.microsoft.com/en-us/windows/win32/power/system-power-states#entering-hibernation) notifica a aplicaciones y servicios antes de guardar la imagen. El temporizador elegido para el UPS no espera confirmación de que Windows haya terminado y una batería baja puede adelantar el corte. Guardar documentos sigue siendo necesario. La simulación representa estas etapas sin ejecutar Windows ni USB.

## Ensayo separado de batería y regreso de entrada

La [prueba manual con batería](ups-battery-test.md) exige primero una cancelación de esta misma unidad registrada mediante observación de una carga independiente. Se autoriza por separado, conserva el bloqueo de producción y usa exclusivamente un corte fijo a tres minutos. No comienza por detectar batería: requiere el botón y la aceptación final del usuario, con la protección automática desactivada y guardada.

Tras registrar durablemente la operación, se envía una única orden de corte y se solicita inmediatamente hibernar Windows sin forzar aplicaciones. Se inicia solo durante una transición reciente a batería, con salida activa, carga moderada y sin alarma baja ni falla. Si Windows rechaza la petición, cualquier intento de cancelar la operación propia conserva su resultado como no confirmado; la interfaz no asegura que la salida vaya a permanecer encendida.

El usuario observa el indicador de alimentación del router para comprobar el corte después de la hibernación. Tras restaurar la entrada, registra si las salidas vuelven automáticamente o si permanecen apagadas y requieren el botón. El retorno automático es aceptado y se conserva como resultado observado. Nunca se deduce retorno automático solo de un intervalo corto ni se deduce finalización de Windows de una notificación de suspensión.

El registro de observación solo guarda metadatos. Los resultados no añaden perfiles automáticamente: el perfil acotado de esta entrega se incorporó por revisión explícita y autorización del usuario. Guardar una observación no activa `policy.ups_poweroff` ni cambia la protección de Windows. La recuperación de lectura, la suspensión, la reanudación y los reinicios no reproducen órdenes.

## Hibernación y reanudación: evidencia del 18 de octubre de 2026

La prueba física en batería `a8b574b3-8bff-4615-80c2-7647c0baced6`, iniciada a las 12:37:17 de Lima, tuvo un envío completo registrado y ninguna cancelación intentada. El usuario registró corte observado, recuperación de la hibernación y encendido automático de las salidas al restablecer la entrada, sin utilizar el botón del UPS y con cero segundos observados de permanencia apagada tras el retorno de CA. Confirmó que el corte tardó unos tres minutos: es una observación aproximada, no una medición exacta de 180 segundos. Es evidencia de temporización aproximada y retorno automático para esta unidad y la orden probada; no se extrapola a todos los NT-2402PRO ni acredita autonomía restante. No se pide repetir el corte ni el retorno de CA para volver a comprobar estos hechos.

La condición anterior que exigía encendido exclusivamente manual fue un error de criterio y se retiró: el usuario acepta expresamente que el UPS vuelva a entregar energía cuando regresa CA. No se reinterpreta su observación como fallo ni se pide otra prueba para satisfacer aquella restricción. Las opciones de tiempo posteriores son configuración operativa autorizada; no modifican la evidencia observada ni inventan condiciones de reserva.

El proceso `shutdown.exe /h` quedó esperando durante la transición y produjo un TimeoutExpired al reanudar; la versión 0.9.8 lo guardó incorrectamente como rechazo de Windows. La observación física del usuario confirma recuperación de la sesión. Se conserva el registro original, pero el panel prioriza el resultado físico final y las nuevas solicitudes distinguen rechazo de resultado sin confirmar. Una transición observada prohíbe cancelar o repetir controles al despertar; no prueba por sí sola que el guardado de hibernación haya terminado.

El usuario realizó exactamente dos pruebas normales de hibernación, informó entre 10 y 15 segundos para entrar en ese estado y recuperó las aplicaciones sin problemas. Los eventos de Windows confirman ambas transiciones a S4. Sus marcas de suspensión y reanudación incluyen el tiempo que el equipo permaneció hibernado; no miden por sí solas el tiempo completo necesario para guardar la imagen. No se solicita una tercera prueba en esta revisión.

Los avisos de comunicación al volver coincidieron con controles de salud que contaron la pausa de Windows como retraso del lector y, en una prueba, con una lectura USB que agotó su plazo durante la reanudación. El servicio debe tratar la suspensión como una pausa esperada y conceder hasta 15 segundos de recuperación al reanudar, manteniendo bloqueadas las acciones hasta obtener telemetría nueva. Si la comunicación no se recupera después de ese plazo, corresponde una alarma de fallo persistente. Los datos anteriores a la suspensión no deben reactivar una cuenta atrás.

Los dos ensayos normales de hibernación conservaron encendido el UPS y no enviaron controles de salida. Los ensayos físicos separados conservan sus resultados observados. Los tiempos configurables no se etiquetan como ensayos nuevos ni aprueban otras unidades, cargas fuera de los límites o acciones no admitidas.
