# Arquitectura de confiabilidad

## Adquisición y persistencia

El hilo `ups-hardware-polling` solo lee el adaptador y publica copias de las muestras en memoria. Un consumidor separado evalúa la protección, genera avisos y solicita persistencia. No se escribe SQLite ni se espera a una ventana desde el hilo de adquisición.

`TelemetryRingBuffer` conserva 256 muestras por defecto. Usa una `SimpleQueue` con un único token de despertar; los datos están en deques limitadas. Un consumidor lento no crea una cola ilimitada. Si se supera la capacidad, conserva contadores y la primera/última fecha de cada tipo conocido de transición, e informa del descarte.

Los datos atrasados no se reproducen como decisiones actuales. Después de retraso o desbordamiento se reinicia la continuidad del motor. Solo la última muestra reciente puede iniciar una acción; el motor vuelve a comprobar su secuencia antes de actuar. Las transiciones intermedias se registran como hechos históricos parciales (`telemetry_delayed_transition`), no como una cronología completa de incidentes.

Las transacciones QS y la enumeración de metadatos HID se ejecutan en procesos internos de propósito fijo. El reporte overlapped tiene un plazo de 2,5 segundos; el padre limita la respuesta del proceso a 3 segundos y la espera de terminación a otros 0,5 segundos. Las API síncronas del controlador no ofrecen cancelación garantizada: si no se confirma la salida del proceso, la ruta USB queda en cuarentena y no se abre otro lector para ella. La reconexión usa espera exponencial limitada a 30 segundos. El transporte sigue permitiendo exclusivamente la consulta de estado QS.

La escritura normal agrupa hasta 32 registros o 30 segundos en una transacción. Las transiciones críticas y las alarmas solicitan inmediatamente al trabajador de persistencia que guarde todos los registros anteriores en orden. SQLite usa WAL y `synchronous=FULL`; las decisiones no esperan al disco. Esto es una solicitud de volcado inmediato, no una garantía frente a un disco fallido o a la terminación del proceso antes del commit. Los ajustes conservan confirmación síncrona.

El cierre no libera la exclusión del monitor ni cierra SQLite mientras siga vivo el lector, el consumidor o el supervisor. Las pruebas usan hardware simulado, bases temporales y callbacks de energía inofensivos.

## Servicio, sesiones y avisos

El servicio es dueño del motor de protección y del callback de energía. El agente interactivo solo recibe avisos y puede solicitar una cancelación. El servicio puede evaluar una batería baja sin ningún agente registrado. La capacidad administrativa `--allow-power-actions` y la política `enabled` son bloqueos independientes; el instalador no habilita ninguno. El modo de prueba del servicio tampoco permite esa capacidad.

El protocolo de avisos v2 separa el cursor de lectura del acuse de entrega. Incluye una época de servicio para rechazar acuses de una ejecución anterior. Un acuse solo procede de una respuesta positiva del backend nativo, después de solicitar un aviso a Windows o mostrar la ventana propia. La aceptación en la cola Python, un sonido o un cursor adelantado no son acuses. Tampoco se afirma que el usuario haya leído el aviso.

El agente consulta WTS para su propia sesión. Una sesión bloqueada, desconectada o de estado desconocido no permite entrega. Al reanudar, el siguiente sondeo —normalmente en un segundo— recupera los avisos retenidos. La retención es limitada, por defecto 32 eventos y 24 horas, y solo existe en memoria hasta que termina el servicio; el historial de disco es independiente. Los diagnósticos informan del descarte por capacidad. No se promete entrega exactamente una vez a través de un cierre o fallo de Windows.

El broker vuelve a construir los avisos a partir del contexto reciente. Un corte recuperado se presenta como registro anterior y una cuenta vencida o cancelada no reutiliza sus segundos antiguos. El notificador invalida tickets pendientes cuando cambia su contenido o la sesión deja de estar disponible. Los acuses perdidos se reintentan y el agente limita los avisos en vuelo.

El notificador Python y el lanzador C# solicitan PMv2 antes de crear ventanas. Las ventanas propias ajustan geometría y fuentes y procesan `WM_DPICHANGED`. Las API ausentes o un contexto ya fijado no impiden iniciar el agente. Estas pruebas de API no sustituyen una revisión visual en el monitor y escalado de destino.

Referencias: [DPI de proceso](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setprocessdpiawarenesscontext), [estado WTSInfoEx](https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/ns-wtsapi32-wtsinfoex_level1_w) y [programas de servicio](https://learn.microsoft.com/en-us/windows/win32/services/service-programs).
